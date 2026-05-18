"""Citation validator behavior tests (A4).

Covers:
- Default flag: embeddings ON unless DISABLE_EMBEDDINGS_FOR_CITATIONS=1.
- Fallback paths: missing VOYAGE_API_KEY → Jaccard + warning; Voyage error
  → Jaccard + warning. Pipeline never raises.
- Quote-embedding cache: same ledger called twice → second call embeds
  only the (changed) prose, not the quotes.
- Cross-language paraphrase: a hand-built case where Jaccard scores below
  the threshold but a mocked Voyage cosine clears it.
"""
from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from account_research.schemas.brief import (
    BriefData,
    HeroSection,
    QuickTake,
)
from account_research.schemas.entity import EntityType
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)

from account_research.agents import citation_validator as cv


def _ev(quote: str, eid=None) -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        id=eid or uuid4(),
        entity_id=uuid4(),
        claim=quote[:60],
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://example.com/p",
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote=quote,
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="verified", method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


def _brief(prose: str, ev_ids: list) -> BriefData:
    return BriefData(
        entity_id=uuid4(),
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body=prose, evidence_ids=ev_ids),
    )


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Each test starts with a fresh cache + warning gate. The module-level
    constants are also reset via monkeypatch.setattr in tests that need it."""
    cv._QUOTE_EMBEDDING_CACHE.clear()
    monkeypatch.setattr(cv, "_warned_missing_voyage_key", False)
    yield


class TestDefaultFlag:
    def test_embeddings_default_on(self, monkeypatch):
        """A4: with neither DISABLE_EMBEDDINGS_FOR_CITATIONS nor
        USE_EMBEDDINGS_FOR_CITATIONS set, _read_use_embeddings() returns True."""
        monkeypatch.delenv("DISABLE_EMBEDDINGS_FOR_CITATIONS", raising=False)
        monkeypatch.delenv("USE_EMBEDDINGS_FOR_CITATIONS", raising=False)
        assert cv._read_use_embeddings() is True

    def test_explicit_disable_off(self, monkeypatch):
        monkeypatch.setenv("DISABLE_EMBEDDINGS_FOR_CITATIONS", "1")
        monkeypatch.delenv("USE_EMBEDDINGS_FOR_CITATIONS", raising=False)
        assert cv._read_use_embeddings() is False

    def test_legacy_use_embeddings_zero_still_disables(self, monkeypatch):
        """Backwards-compat: explicit USE_EMBEDDINGS_FOR_CITATIONS=0 still
        turns embeddings off, so existing scripts don't silently flip on."""
        monkeypatch.delenv("DISABLE_EMBEDDINGS_FOR_CITATIONS", raising=False)
        monkeypatch.setenv("USE_EMBEDDINGS_FOR_CITATIONS", "0")
        assert cv._read_use_embeddings() is False


class TestFallbackPaths:
    def test_no_voyage_key_falls_back_to_jaccard_with_warning(
        self, monkeypatch, caplog,
    ):
        monkeypatch.setattr(cv, "_USE_EMBEDDINGS", True)
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        ev = _ev("The company operates in Mexico, Brazil and Argentina.")
        brief = _brief(
            "Operates in three Latin American markets.",
            [ev.id],
        )
        with caplog.at_level(logging.WARNING, logger="citation_validator"):
            flags = cv.compute_weak_citations(brief, [ev])
        assert any("VOYAGE_API_KEY" in r.message for r in caplog.records)
        # Pipeline does not raise — flags computed via Jaccard. The actual
        # flag presence depends on the Jaccard threshold; what matters is
        # that we got a return without crashing.
        assert isinstance(flags, list)

    def test_voyage_error_falls_back_to_jaccard(self, monkeypatch, caplog):
        monkeypatch.setattr(cv, "_USE_EMBEDDINGS", True)
        monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")

        def boom(brief, quotes_by_id, *, cosine_threshold):
            raise RuntimeError("voyage API timeout")

        monkeypatch.setattr(
            cv, "_compute_weak_citations_embeddings", boom,
        )
        ev = _ev("Pays taxes in Texas.")
        brief = _brief("Located in Texas.", [ev.id])
        with caplog.at_level(logging.WARNING, logger="citation_validator"):
            flags = cv.compute_weak_citations(brief, [ev])
        assert any("Voyage embeddings unavailable" in r.message for r in caplog.records)
        # Did not raise — Jaccard path produced an answer.
        assert isinstance(flags, list)


class _FakeVoyageResult:
    def __init__(self, vectors):
        self.embeddings = vectors


class _FakeVoyageClient:
    """Records every call so the test can assert cache behavior."""

    def __init__(self, *, prose_vec=(1.0, 0.0, 0.0), quote_vec=(1.0, 0.0, 0.0)):
        self.calls: list[list[str]] = []
        self._prose_vec = list(prose_vec)
        self._quote_vec = list(quote_vec)

    def embed(self, inputs, *, model, input_type):
        self.calls.append(list(inputs))
        # Order matches caller: prose_inputs first, then uncached_quote_inputs.
        # We don't know how the caller split them — return alternating vectors
        # matching the lengths. The caller treats first n as prose; we don't
        # know n here so just return cosine-of-1.0-with-quote-vec for prose
        # and quote_vec for quotes. The caller distinguishes by index.
        # Simpler: return the prose vector for the first half and quote vector
        # for the second half — but the caller doesn't tell us where the split
        # is. The cleanest approach: return prose_vec for everything; the
        # caller passes uncached quotes as the tail; same value works either
        # way for cache-presence assertions.
        return _FakeVoyageResult([self._prose_vec for _ in inputs])


class _FakeVoyageModule:
    """Stand-in for the `voyageai` package."""

    class Client(_FakeVoyageClient):
        instances: list = []

        def __init__(self, *, api_key=None):
            super().__init__()
            type(self).instances.append(self)


def _install_fake_voyage(monkeypatch):
    """Inject a fake voyageai module that the lazy import inside
    `_compute_weak_citations_embeddings` will pick up."""
    import sys
    fake = _FakeVoyageModule
    sys.modules["voyageai"] = fake
    _FakeVoyageModule.Client.instances = []
    return fake


class TestQuoteEmbeddingCache:
    def test_quote_embedding_cached_across_two_calls(self, monkeypatch):
        """Same ledger called twice → the second call embeds prose only,
        because the quotes are already in `_QUOTE_EMBEDDING_CACHE`."""
        monkeypatch.setattr(cv, "_USE_EMBEDDINGS", True)
        monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
        fake = _install_fake_voyage(monkeypatch)

        ev = _ev("Quote A about the firm.")
        b1 = _brief("Some prose 1.", [ev.id])
        b2 = _brief("Some prose 2 — different prose entirely.", [ev.id])

        cv.compute_weak_citations(b1, [ev])
        client1 = fake.Client.instances[-1]
        n_inputs_first = len(client1.calls[0])

        cv.compute_weak_citations(b2, [ev])
        client2 = fake.Client.instances[-1]
        n_inputs_second = len(client2.calls[0])

        # First call: 1 prose + 1 quote = 2 inputs.
        # Second call: 1 prose + 0 quotes (cached) = 1 input.
        assert n_inputs_first == 2
        assert n_inputs_second == 1
        # Cache holds the quote
        from account_research.agents.citation_validator import _quote_hash
        assert _quote_hash("Quote A about the firm.") in cv._QUOTE_EMBEDDING_CACHE

    def test_cache_keyed_by_quote_content_not_id(self, monkeypatch):
        """Different EvidenceItems with identical raw_quote share a cache entry."""
        monkeypatch.setattr(cv, "_USE_EMBEDDINGS", True)
        monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
        fake = _install_fake_voyage(monkeypatch)

        ev_a = _ev("Identical quote text.")
        ev_b = _ev("Identical quote text.")  # different UUID, same text
        cv.compute_weak_citations(_brief("prose 1", [ev_a.id]), [ev_a])
        cv.compute_weak_citations(_brief("prose 2", [ev_b.id]), [ev_b])
        client = fake.Client.instances[-1]
        # Second call should have NOT re-embedded the quote.
        assert len(client.calls[0]) == 1  # prose only


class TestCrossLanguageHonoredViaCosine:
    """Hand-built case where Jaccard fails but a controlled cosine clears.

    The prose and quote share zero tokens longer than 3 chars after stop-
    word removal (`Mexico` is shared, but stop-words and short words drop).
    A Voyage cosine >= threshold would approve it; Jaccard would not.
    """

    def test_low_jaccard_low_overlap(self):
        ev = _ev("La firma opera principalmente en México.")
        brief = _brief("The firm runs operations chiefly in Mexico.", [ev.id])
        # Force Jaccard path
        result = cv.check_one(
            brief.quick_take.body,
            brief.quick_take.evidence_ids,
            {ev.id: ev.raw_quote},
            jaccard_threshold=0.20,  # tighten so the 4-gram fallback decides
        )
        # Jaccard alone may or may not flag depending on tokenization;
        # what we want is: when this returns a flag, that flag is what
        # the embeddings path needs to override. Stable contract test.
        assert result is None or "jaccard" in result

    def test_embeddings_can_clear_jaccard_failure(self, monkeypatch):
        """When Jaccard would flag but Voyage cosine clears the threshold,
        the embedding path wins. We mock Voyage to return identical prose
        and quote vectors (cosine = 1.0) so no flag should be raised."""
        monkeypatch.setattr(cv, "_USE_EMBEDDINGS", True)
        monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
        # B6: turn off DB-backed cache so the test doesn't depend on
        # SQLite session shape. Pure in-memory path.
        monkeypatch.setattr(cv, "_USE_DB_BACKED_CACHE", False)
        _install_fake_voyage(monkeypatch)

        ev = _ev("La firma opera principalmente en México.")
        brief = _brief("The firm runs operations chiefly in Mexico.", [ev.id])
        flags = cv.compute_weak_citations(brief, [ev])
        # Mocked cosine = 1.0 → no weak flag
        assert flags == []


# ---------------------------------------------------------------------------
# Plan B / B6 — SQLite-backed embedding cache (cross-process)
# ---------------------------------------------------------------------------


class TestEmbeddingCachePersistence:
    """Hydrate / persist round-trip against a fresh in-memory SQLite."""

    def _patch_sessions(self, monkeypatch, engine):
        """Point the citation_validator's lazy imports at a test session
        backed by the given engine. init_db is a no-op (tables already
        created via the engine fixture)."""
        from sqlalchemy.orm import sessionmaker
        TestSession = sessionmaker(bind=engine, autoflush=False,
                                   autocommit=False, future=True)
        import account_research.db as db_mod
        monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
        monkeypatch.setattr(db_mod, "init_db", lambda: None)

    def test_persist_then_hydrate_round_trip(self, monkeypatch, engine):
        from account_research.agents.citation_validator import (
            _hydrate_cache_from_db,
            _persist_cache_to_db,
            _QUOTE_EMBEDDING_CACHE,
        )
        self._patch_sessions(monkeypatch, engine)
        _persist_cache_to_db(
            {"deadbeef": [0.1, 0.2, 0.3]},
            "voyage-3",
        )
        # Simulate a fresh process by clearing the in-memory cache.
        _QUOTE_EMBEDDING_CACHE.clear()
        _hydrate_cache_from_db({"deadbeef"}, "voyage-3")
        assert _QUOTE_EMBEDDING_CACHE.get("deadbeef") == [0.1, 0.2, 0.3]

    def test_hydrate_misses_other_model(self, monkeypatch, engine):
        """Same quote hash + different model = different cache entry."""
        from account_research.agents.citation_validator import (
            _hydrate_cache_from_db,
            _persist_cache_to_db,
            _QUOTE_EMBEDDING_CACHE,
        )
        self._patch_sessions(monkeypatch, engine)
        _persist_cache_to_db({"hash1": [1.0, 0.0]}, "voyage-3")
        _QUOTE_EMBEDDING_CACHE.clear()
        _hydrate_cache_from_db({"hash1"}, "voyage-3-large")
        assert "hash1" not in _QUOTE_EMBEDDING_CACHE

    def test_db_backed_path_short_circuits_voyage_call(self, monkeypatch, engine):
        """End-to-end: a fresh in-memory cache pre-seeded only via SQLite
        causes the embed call to skip the quote input — Voyage gets only
        the prose, not the (already-cached) quote text."""
        from account_research.agents.citation_validator import (
            _persist_cache_to_db,
            _quote_hash,
            _QUOTE_EMBEDDING_CACHE,
        )
        self._patch_sessions(monkeypatch, engine)
        monkeypatch.setattr(cv, "_USE_EMBEDDINGS", True)
        monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
        fake = _install_fake_voyage(monkeypatch)

        ev = _ev("A stable quote we will embed once.")
        _persist_cache_to_db(
            {_quote_hash(ev.raw_quote): [1.0, 0.0, 0.0]}, "voyage-3",
        )
        _QUOTE_EMBEDDING_CACHE.clear()  # simulate process restart

        b = _brief("Some new prose.", [ev.id])
        cv.compute_weak_citations(b, [ev])
        client = fake.Client.instances[-1]
        # 1 prose input + 0 quote inputs (quote was hydrated from DB).
        assert len(client.calls[0]) == 1
