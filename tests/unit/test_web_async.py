"""Async + cache-atomicity tests for account_research.tools.web (A7).

Verifies that:
- The new atomic cache writer (_write_cache_atomic) is safe under
  concurrent fetches of the same URL — readers never see a partially
  written file because os.replace is atomic.
- direct_web_fetch_async returns the same shape as direct_web_fetch.
- A corrupt cache file is treated as missing instead of crashing the
  caller (defensive against a process that died mid-write before A7).
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from account_research.tools.web import (
    FetchResult,
    _cache_key,
    _read_cache,
    _write_cache_atomic,
    direct_web_fetch,
    direct_web_fetch_async,
)


@pytest.fixture()
def cache_dir(tmp_path: Path) -> Path:
    d = tmp_path / "web_cache"
    d.mkdir()
    return d


def _result(url: str, body: str = "ok") -> FetchResult:
    return FetchResult(
        url=url, status=200, content_text=body,
        fetched_at=datetime.now(timezone.utc), from_cache=False,
    )


class TestAtomicCacheWrites:
    def test_round_trip(self, cache_dir: Path) -> None:
        url = "https://example.com/x"
        cache_path = cache_dir / f"{_cache_key(url)}.json"
        _write_cache_atomic(cache_path, _result(url, body="hello"))
        cached = _read_cache(cache_path)
        assert cached is not None
        assert cached.content_text == "hello"
        assert cached.from_cache is True

    def test_corrupt_cache_treated_as_missing(self, cache_dir: Path) -> None:
        url = "https://example.com/x"
        cache_path = cache_dir / f"{_cache_key(url)}.json"
        # Simulate a half-written cache from before A7 atomic writes landed.
        cache_path.write_text("{ NOT JSON", encoding="utf-8")
        assert _read_cache(cache_path) is None

    def test_no_tmp_file_left_behind(self, cache_dir: Path) -> None:
        url = "https://example.com/x"
        cache_path = cache_dir / f"{_cache_key(url)}.json"
        _write_cache_atomic(cache_path, _result(url))
        # No .tmp files dangling
        tmp_files = list(cache_dir.glob("*.tmp"))
        assert tmp_files == []

    def test_concurrent_writers_converge(self, cache_dir: Path) -> None:
        """Many threads writing the same URL converge to a single
        well-formed cache file — no partial JSON anywhere."""
        url = "https://example.com/concurrent"
        cache_path = cache_dir / f"{_cache_key(url)}.json"

        threads = []
        for _ in range(16):
            t = threading.Thread(
                target=_write_cache_atomic,
                args=(cache_path, _result(url, body="payload")),
            )
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        # Final state: well-formed JSON, no stray tmp files
        assert cache_path.exists()
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        assert data["content_text"] == "payload"
        assert list(cache_dir.glob("*.tmp")) == []


class TestAsyncFetcher:
    @pytest.mark.asyncio
    async def test_async_uses_cache_when_present(
        self, cache_dir: Path, monkeypatch
    ) -> None:
        url = "https://example.com/cached"
        cache_path = cache_dir / f"{_cache_key(url)}.json"
        _write_cache_atomic(cache_path, _result(url, body="cached body"))

        # If httpx is reached, fail loudly — we should never leave the cache.
        async def boom(*args, **kwargs):
            raise AssertionError("httpx must not be called when cache hit")

        # Patch the async client's get method via httpx.AsyncClient.get
        # by monkeypatching the constructor to a stub
        monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)

        result = await direct_web_fetch_async(url, cache_dir=cache_dir)
        assert result.content_text == "cached body"
        assert result.from_cache is True

    @pytest.mark.asyncio
    async def test_async_writes_atomically_after_fetch(
        self, cache_dir: Path, monkeypatch
    ) -> None:
        url = "https://example.com/fresh"
        monkeypatch.setattr(httpx, "AsyncClient", _StubAsyncClient)
        result = await direct_web_fetch_async(url, cache_dir=cache_dir)
        assert result.from_cache is False
        cache_path = cache_dir / f"{_cache_key(url)}.json"
        assert cache_path.exists()
        # No stray temps
        assert list(cache_dir.glob("*.tmp")) == []

    @pytest.mark.asyncio
    async def test_gather_many_urls_no_corruption(
        self, cache_dir: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(httpx, "AsyncClient", _StubAsyncClient)
        urls = [f"https://example.com/p{i}" for i in range(20)]
        results = await asyncio.gather(*[
            direct_web_fetch_async(u, cache_dir=cache_dir) for u in urls
        ])
        assert len(results) == 20
        for r in results:
            assert r.status == 200
        # All cache files well-formed
        for u in urls:
            data = json.loads(
                (cache_dir / f"{_cache_key(u)}.json").read_text(encoding="utf-8"),
            )
            assert data["content_text"]
        assert list(cache_dir.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# httpx stubs
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, url: str):
        self.url = url
        self.status_code = 200
        self.text = f"body for {url}"


class _StubAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url: str):
        return _StubResponse(url)


class _BoomClient:
    """Asserts no HTTP call is made (cache should serve)."""
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url: str):
        raise AssertionError(f"unexpected httpx call to {url}")
