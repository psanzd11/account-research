"""Fact-Checker: exact / fuzzy / dead-source paths and injected-quote detection."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from account_research.agents.base import PipelineContext
from account_research.agents.fact_checker import (
    FUZZY_THRESHOLD,
    FactCheckerAgent,
    FactCheckInput,
)
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceItem,
    SourceType,
)
from account_research.tools.web import FetchResult


def _ev(quote: str, url: str = "https://example.com/p") -> EvidenceItem:
    return EvidenceItem(
        entity_id=uuid4(),
        claim="Some claim",
        category=EvidenceCategory.COMPANY_FACTS,
        source_url=url,
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote=quote,
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
    )


def _patch_fetch(monkeypatch, content: str, status: int = 200, raise_exc: Exception | None = None):
    def fake(url, *, use_cache=True, **_kw):
        if raise_exc is not None:
            raise raise_exc
        return FetchResult(
            url=url,
            status=status,
            content_text=content,
            fetched_at=datetime.now(timezone.utc),
            from_cache=False,
        )
    monkeypatch.setattr("account_research.agents.fact_checker.direct_web_fetch", fake)


def test_exact_substring_verifies(monkeypatch):
    page = "<html>...<p>Founded in 2020 in the Dominican Republic.</p>...</html>"
    _patch_fetch(monkeypatch, page)

    items = [_ev("Founded in 2020 in the Dominican Republic.")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())

    assert result.report.verified == 1
    assert result.items[0].verification.status == "verified"
    assert result.items[0].verification.method == "exact_match"


def test_whitespace_drift_still_verifies(monkeypatch):
    # Page has extra whitespace + HTML entities the quote doesn't
    page = "<p>Desde nuestros&nbsp; inicios   en\n2020 en la República Dominicana.</p>"
    _patch_fetch(monkeypatch, page)

    items = [_ev("Desde nuestros inicios en 2020 en la República Dominicana.")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())

    assert result.items[0].verification.status == "verified"
    assert result.items[0].verification.method == "fuzzy_match"


def test_injected_fake_quote_caught(monkeypatch):
    """A quote that doesn't appear anywhere on the page must be unverifiable.
    This is the SPEC §7 acceptance criterion for the Fact-Checker."""
    page = "<p>This page is entirely about cats.</p>"
    _patch_fetch(monkeypatch, page)

    items = [_ev("Founded in 2020 by a stealth-mode space-rocket startup.")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())

    assert result.items[0].verification.status == "unverifiable"
    assert result.report.verified == 0
    assert result.flagged_low_verification


def test_source_dead_on_404(monkeypatch):
    _patch_fetch(monkeypatch, "Not found", status=404)
    items = [_ev("Anything")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status == "source_dead"
    assert result.items[0].verification.method == "url_404"
    assert result.report.source_dead == 1


def test_source_dead_on_network_error(monkeypatch):
    _patch_fetch(monkeypatch, "", raise_exc=httpx.ConnectError("dns fail"))
    items = [_ev("Anything")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status == "source_dead"


def test_fuzzy_match_typo_within_threshold(monkeypatch):
    # Page has "consolidado" misspelled as "consolidados" — small edit distance
    page = "<p>Desde nuestros inicios en 2020, hemos consolidados nuestra posición como el socio de confianza en gestión de proyectos.</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev("Desde nuestros inicios en 2020, hemos consolidado nuestra posición como el socio de confianza")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    # Should be verified via fuzzy with similarity >= threshold
    assert result.items[0].verification.status == "verified"
    sim = result.items[0].verification.similarity
    assert sim is not None and sim >= FUZZY_THRESHOLD


def test_haiku_fallback_rescues_js_rendered_page(monkeypatch):
    """direct_web_fetch returns a JS skeleton; the Haiku-routed web_fetch
    returns the rendered DOM containing the quote → item flips to verified."""
    skeleton = "<html><head></head><body><div id='root'></div></body></html>"
    rendered = "<p>Founded in 2020 in the Dominican Republic.</p>"

    def fake_direct(url, **kw):
        return FetchResult(url=url, status=200, content_text=skeleton,
                           fetched_at=datetime.now(timezone.utc), from_cache=False)

    def fake_anthropic(url, llm_client, **kw):
        return FetchResult(url=url, status=200, content_text=rendered,
                           fetched_at=datetime.now(timezone.utc), from_cache=False)

    monkeypatch.setattr("account_research.agents.fact_checker.direct_web_fetch", fake_direct)
    monkeypatch.setattr("account_research.agents.fact_checker.anthropic_web_fetch", fake_anthropic)

    items = [_ev("Founded in 2020 in the Dominican Republic.")]
    # Provide a non-None llm_client so the fallback path is enabled
    ctx = PipelineContext(llm_client=object())
    result = FactCheckerAgent().run(FactCheckInput(items=items), ctx)

    assert result.items[0].verification.status == "verified"
    assert result.js_fallback_recoveries == 1


def test_haiku_fallback_skipped_when_no_llm_client(monkeypatch):
    """Without ctx.llm_client the fallback path is silently skipped."""
    skeleton = "<html><body><div id='root'></div></body></html>"

    def fake_direct(url, **kw):
        return FetchResult(url=url, status=200, content_text=skeleton,
                           fetched_at=datetime.now(timezone.utc), from_cache=False)

    monkeypatch.setattr("account_research.agents.fact_checker.direct_web_fetch", fake_direct)
    # If the fallback is called we'll know via this sentinel raising
    def boom(*args, **kw):
        raise AssertionError("anthropic_web_fetch must not be called when llm_client=None")
    monkeypatch.setattr("account_research.agents.fact_checker.anthropic_web_fetch", boom)

    items = [_ev("Founded in 2020.")]
    ctx = PipelineContext(llm_client=None)
    result = FactCheckerAgent().run(FactCheckInput(items=items), ctx)
    assert result.items[0].verification.status == "unverifiable"
    assert result.js_fallback_recoveries == 0


def test_haiku_fallback_disabled_via_input_flag(monkeypatch):
    skeleton = "<html><body></body></html>"

    def fake_direct(url, **kw):
        return FetchResult(url=url, status=200, content_text=skeleton,
                           fetched_at=datetime.now(timezone.utc), from_cache=False)

    monkeypatch.setattr("account_research.agents.fact_checker.direct_web_fetch", fake_direct)
    def boom(*args, **kw):
        raise AssertionError("anthropic_web_fetch must not be called when use_js_fallback=False")
    monkeypatch.setattr("account_research.agents.fact_checker.anthropic_web_fetch", boom)

    items = [_ev("Founded in 2020.")]
    ctx = PipelineContext(llm_client=object())
    result = FactCheckerAgent().run(
        FactCheckInput(items=items, use_js_fallback=False), ctx,
    )
    assert result.js_fallback_recoveries == 0


def test_mixed_batch_metrics(monkeypatch):
    """Ensure verification_rate computes correctly across mixed outcomes."""
    pages = {
        "https://a.example/": "<p>quote one is on this page</p>",
        "https://b.example/": "<p>quote two is on this page</p>",
        "https://c.example/": "<p>completely unrelated content</p>",
    }
    def fake(url, **kw):
        return FetchResult(
            url=url, status=200, content_text=pages.get(url, ""),
            fetched_at=datetime.now(timezone.utc), from_cache=False,
        )
    monkeypatch.setattr("account_research.agents.fact_checker.direct_web_fetch", fake)

    items = [
        _ev("quote one is on this page", "https://a.example/"),
        _ev("quote two is on this page", "https://b.example/"),
        _ev("quote three is NOT on its page", "https://c.example/"),
    ]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.report.verified == 2
    assert result.report.unverifiable == 1
    assert result.report.verification_rate == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# Phase 1 (A1/A3/A4) + Phase 2 (A2) tests
# ---------------------------------------------------------------------------


def test_smart_quote_normalizes(monkeypatch):
    """Smart quotes on the page must not block a verification when the quote
    in the ledger uses ASCII quotes."""
    page = '<p>The CEO said “we operate in three countries” during the call.</p>'
    _patch_fetch(monkeypatch, page)
    items = [_ev('"we operate in three countries"')]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status == "verified"


def test_em_dash_normalizes(monkeypatch):
    """Em-dash on page vs hyphen in quote → still verifies."""
    page = "<p>The strategy was clear — expand into LATAM.</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev("The strategy was clear - expand into LATAM.")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status == "verified"


def test_transliteration_match(monkeypatch):
    """Diacritics on page vs none in quote (or vice-versa) → still verifies."""
    page = "<p>Operations span Mexico, Republica Dominicana, and Argentina.</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev("Operations span Mexico, República Dominicana, and Argentina.")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status == "verified"


def test_ellipsis_unicode_normalizes(monkeypatch):
    page = "<p>The deal closed quickly… within weeks of the term sheet.</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev("The deal closed quickly... within weeks of the term sheet.")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status == "verified"


def test_js_domain_skips_direct_fetch(monkeypatch):
    """LinkedIn URL → direct_web_fetch must NOT be called; Haiku is used first."""
    rendered = "<p>Senior Product Manager at Acme Corp since 2020.</p>"
    direct_calls: list[str] = []
    haiku_calls: list[str] = []

    def fake_direct(url, **kw):
        direct_calls.append(url)
        return FetchResult(url=url, status=200, content_text="<html>skeleton</html>",
                           fetched_at=datetime.now(timezone.utc), from_cache=False)

    def fake_haiku(url, llm_client, **kw):
        haiku_calls.append(url)
        return FetchResult(url=url, status=200, content_text=rendered,
                           fetched_at=datetime.now(timezone.utc), from_cache=False)

    monkeypatch.setattr("account_research.agents.fact_checker.direct_web_fetch", fake_direct)
    monkeypatch.setattr("account_research.agents.fact_checker.anthropic_web_fetch", fake_haiku)

    items = [_ev(
        "Senior Product Manager at Acme Corp since 2020.",
        url="https://www.linkedin.com/in/some-user",
    )]
    ctx = PipelineContext(llm_client=object())
    result = FactCheckerAgent().run(FactCheckInput(items=items), ctx)

    assert result.items[0].verification.status == "verified"
    assert haiku_calls == ["https://www.linkedin.com/in/some-user"]
    assert direct_calls == [], "direct_web_fetch should be skipped for JS-heavy domains"


def test_js_domain_falls_back_to_direct_when_haiku_empty(monkeypatch):
    """If Haiku returns empty for a JS-heavy domain, fall through to direct_web_fetch
    so we still get a source_dead / unverifiable signal instead of crashing."""
    direct_calls: list[str] = []

    def fake_direct(url, **kw):
        direct_calls.append(url)
        return FetchResult(url=url, status=200, content_text="<html><body></body></html>",
                           fetched_at=datetime.now(timezone.utc), from_cache=False)

    def fake_haiku(url, llm_client, **kw):
        return FetchResult(url=url, status=200, content_text="",
                           fetched_at=datetime.now(timezone.utc), from_cache=False)

    monkeypatch.setattr("account_research.agents.fact_checker.direct_web_fetch", fake_direct)
    monkeypatch.setattr("account_research.agents.fact_checker.anthropic_web_fetch", fake_haiku)

    items = [_ev("anything", url="https://crunchbase.com/organization/foo")]
    ctx = PipelineContext(llm_client=object())
    result = FactCheckerAgent().run(FactCheckInput(items=items), ctx)

    assert direct_calls, "direct_web_fetch should be invoked when Haiku returns empty"
    assert result.items[0].verification.status in ("unverifiable", "source_dead")


def test_lowered_fuzzy_threshold_accepts_078_match(monkeypatch):
    """A quote that scores in the 0.78-0.85 range — rejected at v1's 0.85, accepted at v2's 0.78."""
    page = "<p>the company processed approximately five billion USD in transactions during 2024</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev("the company processed about five billion dollars in transactions during 2024")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status == "verified"
    sim = result.items[0].verification.similarity
    assert sim is not None and 0.78 <= sim < 0.95


def test_fake_quote_still_caught_at_v2_threshold(monkeypatch):
    """Lowered threshold must NOT verify a wholly-fabricated quote."""
    page = "<p>The company makes enterprise software for the finance industry.</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev("The company invented a teleportation device in their spare time")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status == "unverifiable"
