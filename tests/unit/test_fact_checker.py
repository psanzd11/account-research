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
    """A quote that doesn't appear anywhere on the page is rejected (A5).

    Pre-A5 this returned ``unverifiable``; under the LCS floor it now
    returns ``rejected`` because the page text shares neither an anchor
    window nor enough longest-common-subseq with the quote. The SPEC §7
    acceptance criterion is unchanged: the Fact-Checker catches the
    injected quote.
    """
    page = "<p>This page is entirely about cats.</p>"
    _patch_fetch(monkeypatch, page)

    items = [_ev("Founded in 2020 by a stealth-mode space-rocket startup.")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())

    assert result.items[0].verification.status == "rejected"
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
    """Without ctx.llm_client the fallback path is silently skipped.

    A5 note: the empty skeleton vs a real quote yields lcs ≈ 0 and
    fuzzy ≈ 0, so the item is now ``rejected`` rather than
    ``unverifiable``. The skipped-fallback assertion still holds.
    """
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
    # A5: empty page + real quote → rejected, not unverifiable
    assert result.items[0].verification.status in ("rejected", "unverifiable")
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
    # B3: the fabricated quote on page c lands in `rejected`, not
    # `unverifiable`. The verification_rate is unchanged (2/3 verified).
    assert (result.report.unverifiable + result.report.rejected) == 1
    assert result.report.rejected == 1
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
    # A5: empty body + real quote can also fall through to ``rejected``.
    assert result.items[0].verification.status in ("rejected", "unverifiable", "source_dead")


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
    """Lowered threshold must NOT verify a wholly-fabricated quote.

    A5: a fabricated quote that shares no anchor and very little LCS with
    the page falls into the ``rejected`` bucket (stricter than the prior
    ``unverifiable``). Either outcome catches the fabrication.
    """
    page = "<p>The company makes enterprise software for the finance industry.</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev("The company invented a teleportation device in their spare time")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status in ("rejected", "unverifiable")


# ---------------------------------------------------------------------------
# Phase 2 A5 — LCS floor: subtly modified quotes are REJECTED, not verified
# ---------------------------------------------------------------------------


def test_identical_raw_quote_is_verified(monkeypatch):
    """A5 Case A: raw_quote identical to the page text → verified, with a
    high claim_similarity_score."""
    page = "<p>The company has 250 employees across four offices in Spain.</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev("The company has 250 employees across four offices in Spain.")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status == "verified"
    # exact_match path → claim_similarity_score = 1.0
    assert result.items[0].verification.claim_similarity_score == 1.0


def test_unicode_normalized_quote_is_verified(monkeypatch):
    """A5 Case B: raw_quote with trivial Unicode differences (smart quotes,
    accents, NBSP) normalizes to a page match and stays verified."""
    page = "<p>El consultor opero principalmente en Mexico durante 5 anos.</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev(
        # Smart-quote + accent + non-breaking space — should normalize away.
        "El consultor operó principalmente en México durante 5 años."
    )]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status == "verified"


def test_two_word_change_is_rejected(monkeypatch):
    """A5 Case C: two words changed → ratio falls below both the 0.78 fuzzy
    floor and the 0.92 LCS floor → REJECTED. Pre-A5 this either passed as
    verified-fuzzy or sat in unverifiable; under A5 it can never enter the
    Author-acceptable ledger via the Tier-1 high fallback.
    """
    # Page wording differs from the quote by two non-trivial words
    # ("contract" → "engagement", "Spain" → "Portugal") plus a small reorder.
    page = (
        "<p>BWPM signed an engagement with the Ministry of Education of "
        "Portugal covering training across 4 regions in 2023.</p>"
    )
    _patch_fetch(monkeypatch, page)
    items = [_ev(
        "BWPM signed a contract with the Ministry of Education of Spain "
        "covering training across 4 regions in 2023."
    )]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    status = result.items[0].verification.status
    # Either rejected (LCS floor fired) OR unverifiable (LCS happened to be
    # high enough). Critically, NOT verified.
    assert status != "verified"


def test_full_paraphrase_is_rejected(monkeypatch):
    """A5 Case D: complete paraphrase shares no anchor and minimal LCS →
    rejected. Author never sees this even if the source is Tier-1."""
    page = (
        "<p>The firm leverages decades of experience to deliver outsized "
        "value to enterprise clients across emerging markets.</p>"
    )
    _patch_fetch(monkeypatch, page)
    items = [_ev(
        "BWPM is a leading Spanish consulting boutique focused on the "
        "public sector with offices in Madrid and Barcelona."
    )]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.items[0].verification.status == "rejected"


def test_rejected_item_blocked_from_author_acceptable_set(monkeypatch):
    """A5: a rejected item — even Tier-1 + HIGH confidence — never returns
    True from is_acceptable_for_author(). The Tier-1 high fallback only
    applies to ``unverifiable``, not to ``rejected``."""
    page = "<p>Some unrelated page content about cats.</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev(
        "Founded in 1998 by a team of ex-NASA engineers in Silicon Valley.",
        url="https://example.com/about",  # Tier-1 official site
    )]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    ev = result.items[0]
    assert ev.verification.status == "rejected"
    # Source IS Tier-1 + HIGH, but rejected status bypasses the fallback.
    from account_research.schemas.evidence import (
        SourceType as _ST, ConfidenceLevel as _CL,
    )
    assert ev.source_type == _ST.OFFICIAL_SITE
    assert ev.confidence == _CL.HIGH
    assert ev.is_acceptable_for_author() is False


def test_claim_similarity_score_persists_to_verification(monkeypatch):
    """A5: every Fact-Checker outcome carries claim_similarity_score
    (None only when no normalization happened — exact_match returns 1.0)."""
    page = "<p>quote content matching exactly</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev("quote content matching exactly")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    sim = result.items[0].verification.claim_similarity_score
    assert sim is not None
    assert sim == 1.0


# ---------------------------------------------------------------------------
# Plan B / B3 — surface rejected count separately in LedgerReport
# ---------------------------------------------------------------------------


def test_rejected_count_surfaced_in_ledger_report(monkeypatch):
    """B3: items rejected by the A5 LCS floor land in
    LedgerReport.rejected, NOT in unverifiable. Pre-B3 they were folded
    into unverifiable and the UI couldn't tell them apart."""
    page = "<p>This page is entirely about cats.</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev("Founded in 2020 by a stealth-mode space-rocket startup.")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.report.rejected == 1
    assert result.report.unverifiable == 0
    assert result.report.verified == 0
    # Total accounting still tied out
    assert (result.report.verified + result.report.unverifiable
            + result.report.rejected + result.report.source_dead) == result.report.total


# ---------------------------------------------------------------------------
# Plan B / B4 — concurrent batch pre-fetch
# ---------------------------------------------------------------------------


def test_batch_prefetch_reuses_monkeypatched_direct_fetch(monkeypatch):
    """B4: the pre-fetch path goes through the same module-level
    ``direct_web_fetch`` symbol, so existing test monkeypatches keep
    working. Each item gets fetched exactly once (deduped by URL).
    """
    call_log: list[str] = []
    pages = {
        "https://a.example/": "<p>quote one is on this page</p>",
        "https://b.example/": "<p>quote two is on this page</p>",
        "https://c.example/": "<p>quote three is on this page</p>",
    }
    def fake(url, **kw):
        call_log.append(url)
        return FetchResult(
            url=url, status=200, content_text=pages.get(url, ""),
            fetched_at=datetime.now(timezone.utc), from_cache=False,
        )
    monkeypatch.setattr("account_research.agents.fact_checker.direct_web_fetch", fake)

    items = [
        _ev("quote one is on this page", "https://a.example/"),
        _ev("quote two is on this page", "https://b.example/"),
        _ev("quote three is on this page", "https://c.example/"),
    ]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    # All three items verified; each URL fetched exactly once.
    assert result.report.verified == 3
    assert sorted(call_log) == sorted(pages.keys())


def test_batch_prefetch_disabled_via_env(monkeypatch):
    """The kill-switch flag falls back to per-item sync fetches without
    changing the output."""
    monkeypatch.setattr(
        "account_research.agents.fact_checker._BATCH_PREFETCH_ENABLED", False,
    )
    page = "<p>quote one is on this page</p>"
    _patch_fetch(monkeypatch, page)
    items = [_ev("quote one is on this page", "https://a.example/")]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.report.verified == 1


def test_batch_prefetch_captures_httpx_errors_as_source_dead(monkeypatch):
    """B4: when the parallel fetch raises an httpx error, the item lands
    in source_dead via the same accounting as the sync path."""
    def fake(url, **kw):
        raise httpx.ConnectError("dns fail")
    monkeypatch.setattr("account_research.agents.fact_checker.direct_web_fetch", fake)
    items = [
        _ev("quote one", "https://a.example/"),
        _ev("quote two", "https://b.example/"),
    ]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.report.source_dead == 2
    assert result.report.verified == 0


def test_unverifiable_and_rejected_split_correctly(monkeypatch):
    """B3 + A5: mixed batch — verified + rejected items land in their own
    buckets; unverifiable stays empty when nothing exhibits the
    high-LCS-low-fuzzy pattern."""
    pages = {
        "https://a.example/": "<p>quote one is on this page</p>",
        "https://b.example/": "<p>completely unrelated content about cats</p>",
    }
    def fake(url, **kw):
        return FetchResult(url=url, status=200, content_text=pages.get(url, ""),
                           fetched_at=datetime.now(timezone.utc), from_cache=False)
    monkeypatch.setattr("account_research.agents.fact_checker.direct_web_fetch", fake)

    items = [
        _ev("quote one is on this page", "https://a.example/"),
        _ev("Founded in 1998 by ex-NASA engineers in Silicon Valley.",
            "https://b.example/"),
    ]
    result = FactCheckerAgent().run(FactCheckInput(items=items), PipelineContext())
    assert result.report.verified == 1
    assert result.report.rejected == 1
    assert result.report.unverifiable == 0
