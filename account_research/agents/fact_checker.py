"""Fact-Checker — re-fetch each source and verify raw_quote is on the page.

SPEC §4.4. No LLM call (httpx + text matching only). Catches:
  - Researcher-side quote hallucinations
  - Stale links (404 / timeout)
  - Content drift since the Researcher fetched it

Verification ladder (first match wins):
  1. exact substring match     → status=verified, method=exact_match, similarity=1.0
  2. normalized substring      → status=verified, method=fuzzy_match, similarity=1.0
     (Unicode-aware: NFKD + diacritic strip + smart-punct map + whitespace)
  3. fuzzy near a head anchor  → status=verified, method=fuzzy_match, similarity>=threshold
  4. fetched but no match      → status=unverifiable, method=content_changed
  5. fetch failed (4xx/timeout)→ status=source_dead,  method=url_404

If verification_rate < 0.7 across the batch, flag for review.

Feature flags (env vars):
  FACT_CHECKER_V2=0                   disable v2 normalization + JS-domain shortcut
  FACT_CHECKER_FUZZY_THRESHOLD=0.78   override tier-3 acceptance ratio
  FACT_CHECKER_BATCH_PREFETCH=0       disable the B4 concurrent prefetch path
                                      (rare — needed only for sync-only tests)
"""
from __future__ import annotations

import asyncio
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Sequence
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from account_research.agents.base import BaseAgent, PipelineContext
from account_research.schemas.evidence import (
    EvidenceItem,
    LedgerReport,
    Verification,
    VerifiedEvidenceItem,
)
from account_research.tools.web import anthropic_web_fetch, direct_web_fetch
from account_research.utils.text_normalize import normalize_for_match, strip_html

FLAG_VERIFICATION_RATE = 0.70

# Feature flag (Phases 1+2): Unicode normalization, lowered fuzzy threshold,
# longer anchor head, and JS-domain shortcut. Default ON because changes are
# strict supersets of v1 behavior (more characters match).
_V2_ENABLED = os.environ.get("FACT_CHECKER_V2", "1").lower() not in ("0", "false", "no", "off")

# A5 (2026-05-18): LCS floor. After the anchored fuzzy match returns < 0.78
# (i.e. would be marked unverifiable), compute a full-page LCS ratio and
# REJECT outright when the ratio also falls below this floor. Catches
# quotes that were subtly modified (or hallucinated) and would otherwise
# slip through via the Tier-1 high-confidence fallback in
# is_acceptable_for_author. Set to 0 (and FACT_CHECKER_LCS_FLOOR_FUZZY=0)
# to disable. Threshold drawn from `improvements-plan.md` A5.
_LCS_FLOOR = float(os.environ.get("FACT_CHECKER_LCS_FLOOR", "0.92"))
_LCS_FLOOR_FUZZY = float(os.environ.get("FACT_CHECKER_LCS_FLOOR_FUZZY", "0.78"))

# B4 (Plan B Phase 2): batch direct_web_fetch calls in parallel via a
# thread pool BEFORE the classify loop. Cuts wall-clock on cold-cache runs
# from O(N×fetch) to O(N/workers × fetch). Each item still goes through
# the same _classify path; only the I/O is concurrent. Default ON; set
# FACT_CHECKER_BATCH_PREFETCH=0 to revert to per-item sequential fetches.
_BATCH_PREFETCH_ENABLED = (
    os.environ.get("FACT_CHECKER_BATCH_PREFETCH", "1").lower()
    not in ("0", "false", "no", "off")
)
_BATCH_PREFETCH_CONCURRENCY = int(
    os.environ.get("FACT_CHECKER_BATCH_CONCURRENCY", "8")
)

# Tier-3 acceptance ratio. v1 used 0.85; v2 lowers to 0.78 but compensates with
# a longer anchor head requirement so false-positive rate stays low.
FUZZY_THRESHOLD = float(
    os.environ.get("FACT_CHECKER_FUZZY_THRESHOLD", "0.78" if _V2_ENABLED else "0.85")
)

# Domains known to be JS-rendered or auth-walled in their HTML response.
# direct_web_fetch returns a skeleton/login wall for these; go straight to
# Anthropic web_fetch (Haiku) for verification.
_JS_HEAVY_DOMAINS = {
    "linkedin.com",
    "crunchbase.com",
    "instagram.com",
    "facebook.com",
    "x.com",
    "twitter.com",
    "pitchbook.com",
}


# ---------------------------------------------------------------------------
# Input / output models
# ---------------------------------------------------------------------------


class FactCheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    items: list[EvidenceItem]
    use_cache: bool = True
    use_js_fallback: bool = True  # Haiku web_fetch retry on unverifiable


class FactCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[VerifiedEvidenceItem] = Field(default_factory=list)
    report: LedgerReport
    flagged_low_verification: bool = False
    js_fallback_recoveries: int = 0  # how many items rescued by Haiku fetch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_html(raw: str) -> str:
    """Convert HTML to a flat text blob suitable for substring search."""
    return strip_html(raw)


def _normalize(text: str) -> str:
    """Normalize for fuzzy matching.

    v2 (default): NFKD + diacritic strip + smart-punct map + whitespace collapse.
    v1 (FACT_CHECKER_V2=0): whitespace collapse + lowercase only.
    """
    if _V2_ENABLED:
        return normalize_for_match(text)
    import re as _re
    t = _re.sub(r"\s+", " ", text)
    return t.strip().lower()


def _fuzzy_best(nq: str, npage: str) -> float:
    """Return the best SequenceMatcher ratio for `nq` against any window of
    `npage` that begins near a head anchor.

    v2 uses anchor_len in [10, 20]; v1 used [6, 15]. The longer anchor pairs
    with the lower 0.78 threshold to keep false-positive rate similar to v1.
    """
    if not nq:
        return 0.0
    if _V2_ENABLED:
        anchor_len = min(20, max(10, len(nq) // 4))
    else:
        anchor_len = min(15, max(6, len(nq) // 4))
    head = nq[:anchor_len]
    best = 0.0
    pos = 0
    while pos < len(npage):
        idx = npage.find(head, pos)
        if idx < 0:
            break
        chunk = npage[idx : idx + len(nq) + 40]
        ratio = SequenceMatcher(None, nq, chunk).ratio()
        if ratio > best:
            best = ratio
            if best >= 0.99:
                return best
        pos = idx + 1
    return best


def _is_js_heavy(url: str) -> bool:
    """True if the URL's host matches a known JS-rendered or auth-walled site."""
    try:
        host = urlparse(url).netloc.lower().split(":", 1)[0]
    except Exception:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in _JS_HEAVY_DOMAINS)


def _host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split(":", 1)[0] or "unknown"
    except Exception:
        return "unknown"


@dataclass
class _MatchOutcome:
    status: str
    method: str
    similarity: float | None
    # A5: full-page LCS ratio. Stored alongside ``similarity`` (anchored
    # window ratio) for diagnostics; the LCS floor uses it to reject items
    # that miss BOTH metrics.
    claim_similarity_score: float | None = None


def _classify(quote: str, page_html: str) -> _MatchOutcome:
    page_text = _strip_html(page_html)
    if quote in page_text:
        return _MatchOutcome("verified", "exact_match", 1.0, 1.0)

    nq = _normalize(quote)
    npage = _normalize(page_text)

    if nq and nq in npage:
        return _MatchOutcome("verified", "fuzzy_match", 1.0, 1.0)

    best = _fuzzy_best(nq, npage)
    if best >= FUZZY_THRESHOLD:
        return _MatchOutcome("verified", "fuzzy_match", round(best, 3),
                             round(best, 3))

    # A5: anchored fuzzy match was weak. Run a full-page LCS check and
    # decide between "rejected" (also weak on LCS — the quote is just not
    # on this page) and "unverifiable" (LCS says there IS a substantial
    # overlap, so the page is plausibly the source but rendering may have
    # garbled the match).
    lcs = SequenceMatcher(None, nq, npage).ratio() if nq and npage else 0.0
    lcs_rounded = round(lcs, 3)
    fuzzy_rounded = round(best, 3) if best else None
    if lcs < _LCS_FLOOR and best < _LCS_FLOOR_FUZZY:
        return _MatchOutcome("rejected", "content_changed",
                             fuzzy_rounded, lcs_rounded)
    return _MatchOutcome("unverifiable", "content_changed",
                         fuzzy_rounded, lcs_rounded)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class FactCheckerAgent(BaseAgent[FactCheckInput, FactCheckResult]):
    name = "fact_checker"
    model = ""  # No LLM call
    input_schema = FactCheckInput
    output_schema = FactCheckResult

    def run(self, payload: FactCheckInput, ctx: PipelineContext) -> FactCheckResult:
        verified_items: list[VerifiedEvidenceItem] = []
        verified = 0
        unverifiable = 0
        source_dead = 0
        rejected = 0  # B3: surfaced separately from unverifiable.
        js_recoveries = 0
        can_js_fallback = (
            payload.use_js_fallback and ctx.llm_client is not None
        )
        # A4: per-domain stats for diagnostics
        per_domain: dict[str, Counter] = {}

        # B4: parallel pre-fetch of all non-JS-heavy URLs. Each entry is
        # either a FetchResult, an Exception (for httpx errors → source_dead
        # path), or absent (fall back to a sync fetch in the loop). JS-heavy
        # URLs are intentionally NOT pre-fetched — they go through the Haiku
        # path in the loop below.
        prefetched = _batch_prefetch_urls(
            payload.items,
            use_cache=payload.use_cache,
            can_js_fallback=can_js_fallback,
        )

        for ev in payload.items:
            checked_at = datetime.now(timezone.utc)
            url = str(ev.source_url)
            host = _host_of(url)
            domain_counter = per_domain.setdefault(host, Counter())

            # A3: skip direct_web_fetch for JS-heavy domains; go straight to
            # Anthropic web_fetch via Haiku.
            skip_direct = _V2_ENABLED and _is_js_heavy(url) and can_js_fallback
            outcome: _MatchOutcome | None = None

            if skip_direct:
                try:
                    js_resp = anthropic_web_fetch(url, ctx.llm_client)
                    if js_resp.content_text:
                        outcome = _classify(ev.raw_quote, js_resp.content_text)
                        if outcome.status == "verified":
                            js_recoveries += 1
                            ctx.logger.info(
                                "Fact-Checker: JS-heavy %s verified via Haiku", url,
                            )
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.warning(
                        "Fact-Checker: Haiku direct-fetch failed for JS-heavy %s: %s",
                        url, exc,
                    )
                    outcome = None
                # If Haiku returned nothing usable, fall through to direct_web_fetch
                # below so we still get a source_dead vs unverifiable signal.

            if outcome is None:
                # B4: prefer the pre-fetched result (parallel batch) when
                # available. Falls back to a synchronous direct_web_fetch
                # only if the batch was disabled or the URL was skipped
                # (e.g. JS-heavy path that flowed through to here).
                pre = prefetched.get(url) if prefetched else None
                if pre is not None:
                    if isinstance(pre, Exception):
                        ctx.logger.warning("source_dead %s: %s", ev.source_url, pre)
                        ver = Verification(
                            status="source_dead", method="url_404",
                            checked_at=checked_at,
                        )
                        verified_items.append(_attach(ev, ver))
                        source_dead += 1
                        domain_counter["source_dead"] += 1
                        continue
                    resp = pre
                else:
                    try:
                        resp = direct_web_fetch(url, use_cache=payload.use_cache)
                    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                        ctx.logger.warning("source_dead %s: %s", ev.source_url, exc)
                        ver = Verification(
                            status="source_dead", method="url_404",
                            checked_at=checked_at,
                        )
                        verified_items.append(_attach(ev, ver))
                        source_dead += 1
                        domain_counter["source_dead"] += 1
                        continue

                if resp.status >= 400:
                    ver = Verification(
                        status="source_dead", method="url_404", checked_at=checked_at
                    )
                    verified_items.append(_attach(ev, ver))
                    source_dead += 1
                    domain_counter["source_dead"] += 1
                    continue

                outcome = _classify(ev.raw_quote, resp.content_text)

                # JS-rendered pages: direct_web_fetch returns a skeleton, the
                # quote isn't in the HTML but lives in the rendered DOM. Retry
                # via Anthropic's server-side web_fetch (Haiku 4.5, ~$0.02/call).
                # A5: also retry on ``rejected`` — a skeleton-only direct fetch
                # often scores very low on both fuzzy and LCS, but Haiku's
                # rendered DOM may surface the actual quote.
                if (outcome.status in ("unverifiable", "rejected")
                        and can_js_fallback and not skip_direct):
                    try:
                        js_resp = anthropic_web_fetch(url, ctx.llm_client)
                        if js_resp.content_text:
                            retry_outcome = _classify(ev.raw_quote, js_resp.content_text)
                            if retry_outcome.status == "verified":
                                outcome = retry_outcome
                                js_recoveries += 1
                                ctx.logger.info(
                                    "Fact-Checker: rescued %s via Haiku web_fetch", url,
                                )
                    except Exception as exc:  # noqa: BLE001
                        ctx.logger.warning(
                            "Fact-Checker: Haiku fallback failed for %s: %s",
                            url, exc,
                        )

            ver = Verification(
                status=outcome.status,  # type: ignore[arg-type]
                method=outcome.method,  # type: ignore[arg-type]
                checked_at=checked_at,
                similarity=outcome.similarity,
                claim_similarity_score=outcome.claim_similarity_score,
            )
            verified_items.append(_attach(ev, ver))
            if outcome.status == "verified":
                verified += 1
                domain_counter["verified"] += 1
            elif outcome.status == "source_dead":
                source_dead += 1
                domain_counter["source_dead"] += 1
            elif outcome.status == "rejected":
                # A5 + B3: rejected items count separately from unverifiable.
                # They never enter the Author-acceptable set, regardless of
                # source tier. B3 surfaces this distinction up to the
                # LedgerReport so the Library UI can render "rejected" vs
                # "unverifiable" as distinct columns.
                rejected += 1
                domain_counter["rejected"] += 1
            else:
                unverifiable += 1
                domain_counter["unverifiable"] += 1

        report = LedgerReport(
            total=len(payload.items),
            verified=verified,
            unverifiable=unverifiable,
            source_dead=source_dead,
            rejected=rejected,
        )
        flagged = report.verification_rate < FLAG_VERIFICATION_RATE if report.total else False
        if flagged:
            ctx.logger.warning(
                "Fact-Checker: verification rate %.2f below %.2f - flagging",
                report.verification_rate,
                FLAG_VERIFICATION_RATE,
            )

        # A4: log per-domain stats (verified / unverifiable / source_dead)
        if per_domain:
            top = sorted(
                per_domain.items(),
                key=lambda kv: -sum(kv[1].values()),
            )[:10]
            summary = ", ".join(
                f"{h}: {dict(c)}" for h, c in top
            )
            ctx.logger.info("Fact-Checker per-domain (top 10): %s", summary)

        return FactCheckResult(
            items=verified_items, report=report,
            flagged_low_verification=flagged,
            js_fallback_recoveries=js_recoveries,
        )


def _attach(ev: EvidenceItem, verification: Verification) -> VerifiedEvidenceItem:
    """Build a VerifiedEvidenceItem from an EvidenceItem + Verification."""
    return VerifiedEvidenceItem(
        id=ev.id,
        entity_id=ev.entity_id,
        claim=ev.claim,
        category=ev.category,
        source_url=ev.source_url,
        source_type=ev.source_type,
        raw_quote=ev.raw_quote,
        fetched_at=ev.fetched_at,
        confidence=ev.confidence,
        notes=ev.notes,
        verification=verification,
    )


def fact_check_items(
    items: Sequence[EvidenceItem],
    ctx: PipelineContext,
    *,
    use_cache: bool = True,
) -> FactCheckResult:
    """Convenience wrapper used by the CLI and orchestrator."""
    return FactCheckerAgent().run(
        FactCheckInput(items=list(items), use_cache=use_cache), ctx
    )


def _batch_prefetch_urls(
    items: Sequence[EvidenceItem],
    *,
    use_cache: bool,
    can_js_fallback: bool,
) -> dict[str, "object"]:
    """Pre-fetch all eligible URLs concurrently via a thread pool.

    Returns a dict ``url → FetchResult | Exception``. URLs that flow
    through the Haiku JS path are intentionally absent — the caller
    handles them in the main loop. Exceptions are captured rather than
    raised so the per-item ``source_dead`` accounting still reflects the
    correct host.

    Concurrency uses ``asyncio.to_thread`` on the existing sync
    ``direct_web_fetch`` so monkeypatches in unit tests still apply (the
    name is resolved lazily from this module's globals at call time).
    """
    if not _BATCH_PREFETCH_ENABLED or len(items) <= 1:
        return {}

    # Dedup by URL — same source cited by multiple items only needs one fetch.
    targets: list[str] = []
    seen: set[str] = set()
    for ev in items:
        url = str(ev.source_url)
        if url in seen:
            continue
        seen.add(url)
        # JS-heavy hosts skip the direct fetch entirely when the Haiku
        # fallback is available; mirror that here so we don't burn an HTTP
        # round-trip on a page we know returns a skeleton.
        if _V2_ENABLED and _is_js_heavy(url) and can_js_fallback:
            continue
        targets.append(url)

    if not targets:
        return {}

    async def _gather() -> dict[str, object]:
        sem = asyncio.Semaphore(_BATCH_PREFETCH_CONCURRENCY)

        async def _one(url: str):
            async with sem:
                try:
                    # ``direct_web_fetch`` resolves via module globals at
                    # call time — supports test monkeypatching naturally.
                    return url, await asyncio.to_thread(
                        direct_web_fetch, url, use_cache=use_cache,
                    )
                except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                    return url, exc

        pairs = await asyncio.gather(*[_one(u) for u in targets])
        return dict(pairs)

    try:
        return asyncio.run(_gather())
    except RuntimeError:
        # Already inside an event loop (rare — Streamlit + async CLI hybrid).
        # Fall back to empty dict; the caller loop will fetch sequentially.
        return {}
