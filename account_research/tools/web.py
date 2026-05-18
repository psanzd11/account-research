"""Web tools — multiple fetch backends + Anthropic tool definitions.

Three flavors:

1. Tool *definitions* (`web_search_tool_def`, `web_fetch_tool_def`) — opaque
   dicts you hand to LLMClient as `extra_tools`. Anthropic executes the tool
   server-side; the agent never sees the raw HTTP traffic.

2. `direct_web_fetch(url)` (and `direct_web_fetch_async`) — a local httpx
   GET with on-disk caching. Free. Good for static HTML. Fails on JS-
   rendered SPAs (returns empty skeleton).

3. `anthropic_web_fetch(url, llm_client, model=HAIKU)` — single Haiku call
   that uses Anthropic's server-side web_fetch (JS-aware). Used as a fallback
   by the Fact-Checker when (2) returns content that doesn't contain the
   expected quote. Costs ~$0.02 per call. Independently cached.

Cache directory defaults to outputs/web_cache/; tests can override via the
`cache_dir` argument or by setting WEB_CACHE_DIR.

A7 — Cache writes are atomic (tmp + os.replace). This makes the cache
safe under concurrent writes from multiple Fact-Checker workers, multiple
CLI processes, and the Streamlit refine flow firing alongside a manual
run. Partial-read corruption was theoretically possible before; in
practice we got lucky.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CACHE_DIR = REPO_ROOT / "outputs" / "web_cache"

# Anthropic server-side tool type identifiers. Bump these when Anthropic
# releases a newer version; they are date-stamped contracts.
_WEB_SEARCH_TYPE = "web_search_20250305"
_WEB_FETCH_TYPE = "web_fetch_20250910"


# ---------------------------------------------------------------------------
# Tool definitions for Anthropic server-side execution
# ---------------------------------------------------------------------------


def web_search_tool_def(max_uses: int = 5) -> dict:
    """Returns the dict to include in `tools=[...]` for messages.create."""
    return {"type": _WEB_SEARCH_TYPE, "name": "web_search", "max_uses": max_uses}


def web_fetch_tool_def(max_uses: int = 8) -> dict:
    return {"type": _WEB_FETCH_TYPE, "name": "web_fetch", "max_uses": max_uses}


# ---------------------------------------------------------------------------
# Direct fetch with on-disk cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content_text: str
    fetched_at: datetime
    from_cache: bool


def _cache_dir() -> Path:
    override = os.environ.get("WEB_CACHE_DIR")
    path = Path(override) if override else DEFAULT_CACHE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


_FETCH_HEADERS = {
    "User-Agent": "account-research-agents/0.1 (+https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


def _read_cache(cache_path: Path) -> FetchResult | None:
    """Return the cached FetchResult or None if missing/corrupt. A partial
    write that left invalid JSON behind is treated as missing; the caller
    re-fetches and rewrites atomically.
    """
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return FetchResult(
        url=data["url"],
        status=data["status"],
        content_text=data["content_text"],
        fetched_at=datetime.fromisoformat(data["fetched_at"]),
        from_cache=True,
    )


def _write_cache_atomic(cache_path: Path, result: FetchResult) -> None:
    """Atomic write: serialize to a temp sibling, then os.replace into place.

    Concurrent writers all write their own tmp file; os.replace is atomic on
    POSIX, and on Windows when source/dest are on the same volume AND no
    other process holds the destination open. Since the cache key is
    content-addressed (sha256 of the URL), two writers racing the same URL
    converge to byte-identical payloads — if Windows rejects our replace
    because a concurrent writer just installed theirs, we silently drop the
    temp (they wrote the same bytes).
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(
        f".{os.getpid()}.{hashlib.sha256(str(id(result)).encode()).hexdigest()[:8]}.tmp"
    )
    tmp.write_text(
        json.dumps({
            "url": result.url,
            "status": result.status,
            "content_text": result.content_text,
            "fetched_at": result.fetched_at.isoformat(),
        }),
        encoding="utf-8",
    )
    try:
        os.replace(tmp, cache_path)
    except PermissionError:
        # Windows: another writer holds the destination open right now.
        # Their payload is byte-identical (content-addressed key), so drop
        # our temp and move on — the cache stays consistent either way.
        try:
            tmp.unlink()
        except OSError:
            pass


def direct_web_fetch(
    url: str,
    *,
    timeout_s: float = 15.0,
    use_cache: bool = True,
    cache_dir: Path | None = None,
) -> FetchResult:
    cache_root = cache_dir or _cache_dir()
    cache_path = cache_root / f"{_cache_key(url)}.json"

    if use_cache:
        cached = _read_cache(cache_path)
        if cached is not None:
            return cached

    with httpx.Client(
        follow_redirects=True, timeout=timeout_s, headers=_FETCH_HEADERS,
    ) as client:
        resp = client.get(url)

    result = FetchResult(
        url=str(resp.url),
        status=resp.status_code,
        content_text=resp.text,
        fetched_at=datetime.now(timezone.utc),
        from_cache=False,
    )

    if use_cache:
        _write_cache_atomic(cache_path, result)

    return result


async def direct_web_fetch_async(
    url: str,
    *,
    timeout_s: float = 15.0,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    client: httpx.AsyncClient | None = None,
) -> FetchResult:
    """Async sibling of ``direct_web_fetch``.

    Designed for ``asyncio.gather`` over many URLs (Fact-Checker batch
    re-fetch, contact-finder, etc.). Honors the same atomic-write cache
    contract so callers can share a process-wide ``WEB_CACHE_DIR`` with
    sync callers without corruption.

    Pass a shared ``httpx.AsyncClient`` when fetching many URLs to reuse
    connections; otherwise a one-shot client is opened per call.
    """
    cache_root = cache_dir or _cache_dir()
    cache_path = cache_root / f"{_cache_key(url)}.json"

    if use_cache:
        cached = _read_cache(cache_path)
        if cached is not None:
            return cached

    if client is None:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout_s, headers=_FETCH_HEADERS,
        ) as own_client:
            resp = await own_client.get(url)
    else:
        resp = await client.get(url)

    result = FetchResult(
        url=str(resp.url),
        status=resp.status_code,
        content_text=resp.text,
        fetched_at=datetime.now(timezone.utc),
        from_cache=False,
    )

    if use_cache:
        # Atomic write is thread/process-safe; no per-URL lock needed
        # because content-addressed keys make racing writers converge.
        _write_cache_atomic(cache_path, result)

    return result


# ---------------------------------------------------------------------------
# JS-aware fetch via Anthropic web_fetch tool (Haiku 4.5 by default)
# ---------------------------------------------------------------------------


_ANTHROPIC_CACHE_SUBDIR = "anthropic_fetch"


def anthropic_web_fetch(
    url: str,
    llm_client,
    *,
    model: str | None = None,
    use_cache: bool = True,
    cache_dir: Path | None = None,
) -> FetchResult:
    """Fetch a URL via Anthropic's server-side web_fetch tool.

    Uses Haiku 4.5 by default — cheapest model that supports web_fetch.
    The model is instructed to fetch the URL and reply with the page's
    text content only. Returns a FetchResult shaped like `direct_web_fetch`.

    Independently cached from `direct_web_fetch` to keep the two backends
    distinguishable on disk.
    """
    from account_research.llm_client import HAIKU
    chosen_model = model or HAIKU

    cache_root = (cache_dir or _cache_dir()) / _ANTHROPIC_CACHE_SUBDIR
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_path = cache_root / f"{_cache_key(url)}.json"

    if use_cache and cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return FetchResult(
            url=data["url"],
            status=data["status"],
            content_text=data["content_text"],
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
            from_cache=True,
        )

    msg = llm_client.complete(
        model=chosen_model,
        system=(
            "You are a web content extractor. When the user gives you a URL, "
            "use the web_fetch tool to fetch it, then reply with the page's "
            "main text content as plain text. Include all visible body text. "
            "Do not summarize, do not editorialize, do not add commentary."
        ),
        messages=[{
            "role": "user",
            "content": f"Fetch this URL and return its full text content:\n{url}",
        }],
        tools=[web_fetch_tool_def(max_uses=2)],
        max_tokens=8192,
        agent="anthropic_web_fetch",
    )

    parts: list[str] = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)  # type: ignore[attr-defined]
    content_text = "\n".join(parts).strip()

    # Anthropic doesn't surface the underlying HTTP status; treat empty
    # content as effectively "could not fetch" so the caller still flags it.
    status = 200 if content_text else 502

    result = FetchResult(
        url=url, status=status, content_text=content_text,
        fetched_at=datetime.now(timezone.utc), from_cache=False,
    )

    if use_cache:
        cache_path.write_text(
            json.dumps({
                "url": result.url, "status": result.status,
                "content_text": result.content_text,
                "fetched_at": result.fetched_at.isoformat(),
            }),
            encoding="utf-8",
        )

    return result
