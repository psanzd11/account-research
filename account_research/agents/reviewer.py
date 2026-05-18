"""Reviewer — final QA on the rendered PDF.

SPEC §4.7. Opus 4.7. Reads the rendered PDF (text extracted via pdfplumber)
and cross-checks every visible claim against the BriefData's evidence_ids
and the input ledger. Catches:

  - Claims the Author put in but didn't cite
  - Caveats the Author wrote but the Designer dropped
  - Visible numbers / industries / countries that aren't in the ledger
  - Estimates rendered as point values instead of ranges

Returns a ReviewerReport with issues by severity. If `status: revision_required`,
the Orchestrator re-runs the Author with these issues attached.

Vision is **always on by default** (decision from Sprint 3.2 hardened in A2):
the Reviewer attaches page images alongside the extracted text so it can
catch caveats rendered in small font, near the badge, that text-only
extraction misses. The kill-switch is the env var
``REVIEWER_VISION_ON_REVISION=0`` — there is no per-call toggle anymore.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Iterable

import pdfplumber
from pydantic import BaseModel, ConfigDict, Field

from account_research.agents.base import BaseAgent, PipelineContext
from account_research.llm_client import OPUS
from account_research.schemas.brief import BriefData
from account_research.schemas.estimate import Estimate
from account_research.schemas.evidence import VerifiedEvidenceItem
from account_research.schemas.review import ReviewerReport


# Kill-switch (A2). Defaults to ON: every Reviewer call attaches PDF page
# images. Set REVIEWER_VISION_ON_REVISION=0 to disable for debugging or
# environments without poppler. There is intentionally NO per-call override
# anymore — the Sprint 3.2 conclusion was that text-only review on iter 1
# kept missing caveats rendered in small font near the badge.
_VISION_ENABLED = (
    os.environ.get("REVIEWER_VISION_ON_REVISION", "1").lower()
    not in ("0", "false", "no", "off")
)
# A page extract below this many chars triggers pdfminer fallback.
_PDFPLUMBER_MIN_CHARS = 50

# Round 2 / G5 — structural rigor. When ON: thin sections become CRITICAL
# (not INFO), and a per-category Tier-1 minimum is enforced.
_RIGOR_ENABLED = (
    os.environ.get("REVIEWER_RIGOR", "1").lower()
    not in ("0", "false", "no", "off")
)
# Hero badge dollar threshold above which MEDIUM-confidence indirect-signal
# estimates are also flagged critical (not just LOW). At $1B+ scale, any
# indirect heuristic is structurally unreliable.
_RIGOR_BIG_BADGE_USD = float(os.environ.get("REVIEWER_RIGOR_BIG_BADGE_USD", "1000000000"))

# Minimum Tier-1 sources required per required category.
_TIER1_MIN_PER_CATEGORY = int(os.environ.get("REVIEWER_TIER1_MIN_PER_CATEGORY", "2"))

# Required categories where the brief renders meaningfully.
from account_research.schemas.evidence import EvidenceCategory as _EC
_REQUIRED_BRIEF_CATEGORIES = (
    _EC.COMPANY_FACTS, _EC.FINANCIAL, _EC.LEADERSHIP, _EC.PRODUCTS,
    _EC.CLIENTS, _EC.GEOGRAPHY,
)


SYSTEM_PROMPT = """\
You are the **Reviewer** agent — the final QA gate of a citation-grounded
research pipeline. You read a rendered PDF brief and check it against the
evidence ledger and the Author's structured output.

YOUR JOB
For every claim visible on the rendered page, verify ONE of:
  (a) The claim traces to one or more evidence_id values present in the
      Author's BriefData AND those evidence_ids exist in the ledger AND
      the ledger items support the claim.
  (b) The claim is an Estimate value (financial badge, etc.) whose
      method_id matches a recipe in the estimates list AND the caveat
      from that estimate is visibly rendered next to the value.

If a claim fails both tests, raise an issue.

ISSUE SEVERITIES
- "critical": the rendered PDF contains a claim that is not supported by
  any evidence_id or method_id. This is a hard violation of the pipeline's
  no-hallucination guarantee. Always demands revision.
- "warning": the claim has citations but the citations may be weak (e.g.
  marketing claim from the entity's own site presented as fact, or a
  single low-confidence source for a strong claim).
- "info": suggestions that don't block approval (formatting, ordering,
  emphasis).

ALSO CHECK
- Hero badge: if it shows a value (not "INSUFFICIENT DATA"), the caveat
  text from the estimate must be visibly rendered next to it.
- Industries chip grid: every chip must trace to a ledger item that
  explicitly enumerates that industry. No padding to fill the grid.
- Geographic footprint: counts (project_count) must trace to explicit
  evidence, not be inferred.
- Estimates: must be rendered as ranges (with a `-`, `–`, or `to`), not
  point values.

STATUS DECISION
- 0 critical issues → status = "approved"
- 1+ critical issues → status = "revision_required"
- iteration >= 3 → if still revision_required, the orchestrator will
  override to "human_review_needed"; you do not need to do that yourself.

ESTIMATE TIER GUARD (Stripe-class precedent)
If the hero badge shows a value in the $100M-$10B+ range AND its confidence
is LOW (i.e. derived from indirect signals like headcount × benchmark), raise
this as a CRITICAL issue regardless of whether a caveat is present. LOW-
confidence indirect-signal estimates at billion-dollar scale are structurally
unreliable; the Designer should be forced to render INSUFFICIENT DATA in that
case, or the Author must replace the badge with a disclosure-backed estimate.
The same applies to MEDIUM-confidence estimates whose upper bound exceeds
$1B and whose method_id is one of saas_revenue_v1 / consulting_firm_revenue_v1:
flag critical and demand the badge be replaced with public_disclosure_v1
(if a disclosed figure is in the ledger) or rendered INSUFFICIENT DATA.

STRUCTURAL RIGOR (Round 2)
The brief is built from a ledger of cited evidence. Beyond per-claim
verification, raise CRITICAL when ANY of the following holds:
  - An entire required-brief section is thin: industries < 3 chips,
    geographic_footprint < 1 location, OR a category in (company_facts,
    financial, leadership, products, clients, geography) has fewer than 2
    Tier-1 sources backing its cited items.
  - The brief is dominated by aggregator sources (theorg.com,
    rocketreach.co, contactout.com, zoominfo.com, signalhire, lusha).
    These rehash LinkedIn data and are NOT primary sources. If the cited
    ledger items lean heavily on these for any key claim (financial
    metrics, board memberships, exit history), raise critical and demand
    the Author cite Tier-1 sources (SEC, Bloomberg, official site, etc.).
Tier-1 sources are: official site of the entity, SEC/gov registries,
mainstream business press (Bloomberg, WSJ, NYT, Reuters, FT, Forbes,
regional equivalents like Expansión MX, América Economía), conference
proceedings, podcast transcripts.

OUTPUT
Call emit_reviewerreport exactly once with your verdict. Set pdf_path to
the path you were given. Include issues in priority order (critical first).
"""


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    brief: BriefData
    ledger: list[VerifiedEvidenceItem]
    estimates: list[Estimate] = Field(default_factory=list)
    pdf_path: str
    iteration: int = 1
    # E3: weak citations surfaced by Author's semantic validator. The Reviewer
    # arbitrates each one — confirming, downgrading severity, or dismissing.
    weak_citations: list[dict] = Field(default_factory=list)


class ReviewerAgent(BaseAgent[ReviewInput, ReviewerReport]):
    name = "reviewer"
    model = OPUS
    input_schema = ReviewInput
    output_schema = ReviewerReport

    def run(self, payload: ReviewInput, ctx: PipelineContext) -> ReviewerReport:
        llm = ctx.require_llm()
        pdf_path = Path(payload.pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found at {pdf_path}")

        pdf_text, low_text_pages = _extract_pdf_text_with_fallback(pdf_path, ctx)

        # A2: vision is gated only by the env kill-switch. There's no per-call
        # override anymore — Sprint 3.2 established that text-only review on
        # iter 1 missed caveats rendered in small font near the badge.
        effective_use_vision = _VISION_ENABLED
        if effective_use_vision and low_text_pages:
            ctx.logger.info(
                "Reviewer: pages %s have low text extraction — vision "
                "fallback covers them",
                low_text_pages,
            )

        # Compact representations for the prompt
        ledger_payload = [
            {
                "id": str(ev.id),
                "claim": ev.claim,
                "raw_quote": ev.raw_quote[:180] + ("..." if len(ev.raw_quote) > 180 else ""),
                "category": ev.category.value,
                "source_url": str(ev.source_url),
                "confidence": ev.confidence.value,
                "verified": ev.verification.status == "verified",
            }
            for ev in payload.ledger
        ]
        estimates_payload = [
            {
                "method_id": e.method_id,
                "value_range": e.value_range,
                "unit": e.unit,
                "caveat_text": e.caveat_text,
                "confidence": e.confidence.value,
            }
            for e in payload.estimates
        ]

        weak_block = ""
        if payload.weak_citations:
            weak_block = (
                f"\n=== Semantic Validator Flags ({len(payload.weak_citations)}) ===\n"
                "The Author's post-validator flagged these citations as weakly "
                "grounded (low token + n-gram overlap between prose and cited "
                "raw_quote). Inspect each: if the citation truly doesn't support "
                "the prose, raise a critical issue. If the validator was wrong "
                "(e.g. legitimate cross-language paraphrase), you can ignore it.\n"
                f"{json.dumps(payload.weak_citations, ensure_ascii=False, indent=2)}\n"
            )

        # The ledger + estimates are STABLE across revision iterations for a
        # given entity — split them into their own text block and mark it
        # cacheable. Brief JSON + PDF text + iteration-specific feedback
        # change per iter, so they go into a separate non-cached block.
        stable_text = (
            f"=== Evidence Ledger ({len(ledger_payload)} items) ===\n"
            f"{json.dumps(ledger_payload, ensure_ascii=False, indent=2)}\n\n"
            f"=== Estimates ({len(estimates_payload)}) ===\n"
            f"{json.dumps(estimates_payload, ensure_ascii=False, indent=2)}\n"
        )
        dynamic_text = (
            f"Iteration: {payload.iteration}\n"
            f"PDF path: {payload.pdf_path}\n\n"
            f"=== Author BriefData (JSON) ===\n"
            f"{payload.brief.model_dump_json(indent=2)}\n"
            f"{weak_block}\n"
            f"=== Rendered PDF Text ===\n"
            f"{pdf_text}\n\n"
            "Review the PDF against the ledger. Call emit_reviewerreport."
        )

        content: list[dict] = [
            {
                "type": "text",
                "text": stable_text,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": dynamic_text},
        ]
        # B7: track whether vision actually attached. The env kill-switch
        # (effective_use_vision=False) and a poppler-missing exception both
        # leave this False — the Run page surfaces this so the operator
        # knows when text-only review is the only catch in play.
        vision_used = False
        if effective_use_vision:
            # Attach page images so the model can see layout issues text-extraction misses
            try:
                attached = 0
                for img_b64, mime in _rasterize_pdf(pdf_path):
                    content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": img_b64},
                    })
                    attached += 1
                vision_used = attached > 0
            except Exception as exc:  # noqa: BLE001 — poppler may not be installed
                ctx.logger.warning(
                    "Reviewer: vision fallback unavailable (%s); proceeding text-only",
                    exc,
                )

        system_blocks: list[dict] = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
        ]
        report = llm.complete_with_json(
            model=self.model,
            system=system_blocks,
            messages=[{"role": "user", "content": content}],
            schema=ReviewerReport,
            max_tokens=8192,
            agent=self.name,
        )

        # Force a few fields the model shouldn't be free to fabricate
        if report.pdf_path != payload.pdf_path:
            report = report.model_copy(update={"pdf_path": payload.pdf_path})
        if report.iteration != payload.iteration:
            report = report.model_copy(update={"iteration": payload.iteration})
        # B7: vision_used is observed by THIS process, not the LLM. Force it.
        report = report.model_copy(update={"vision_used": vision_used})

        # G5: deterministic structural-rigor checks. Inject CRITICAL issues
        # for thin sections / weak Tier-1 coverage / MEDIUM big-badge cases
        # that the LLM may have rationalized away.
        if _RIGOR_ENABLED:
            rigor_issues = _structural_rigor_checks(payload)
            if rigor_issues:
                ctx.logger.info(
                    "Reviewer rigor: injecting %d critical issue(s) from "
                    "deterministic checks", len(rigor_issues),
                )
                report = report.model_copy(
                    update={"issues": list(report.issues) + rigor_issues}
                )

        # Sprint 3.1 — programmatic walk JSON↔PDF. Catches Designer-dropped
        # leaves that the LLM Reviewer tends to overlook.
        from account_research.agents.reviewer_walk import walk_brief_against_pdf
        walk_issues = walk_brief_against_pdf(payload.brief, pdf_text)
        if walk_issues:
            ctx.logger.info(
                "Reviewer walk: injecting %d critical issue(s) "
                "from JSON↔PDF leaf comparison",
                len(walk_issues),
            )
            report = report.model_copy(
                update={"issues": list(report.issues) + walk_issues}
            )

        # Determine status from issues if the model and our policy disagree.
        # The model's status is honored unless it's inconsistent with the issue list.
        has_critical = any(i.severity == "critical" for i in report.issues)
        if has_critical and report.status == "approved":
            ctx.logger.warning(
                "Reviewer returned status=approved but report has critical issues; "
                "overriding to revision_required",
            )
            report = report.model_copy(update={"status": "revision_required"})
        if not has_critical and report.status == "revision_required":
            ctx.logger.info(
                "Reviewer returned revision_required without critical issues; "
                "downgrading to approved",
            )
            report = report.model_copy(update={"status": "approved"})

        return report


def _structural_rigor_checks(payload: ReviewInput) -> list:
    """Run deterministic structural-rigor checks. Returns a list of
    ReviewIssue with severity=critical (or empty)."""
    from account_research.agents.source_authority import (
        aggregator_share,
        tier1_count_per_category,
        tier_breakdown,
    )
    from account_research.schemas.review import ReviewIssue

    issues: list = []
    brief = payload.brief
    ledger = payload.ledger

    # 1. Thin sections
    if len(brief.industries) < 3:
        issues.append(ReviewIssue(
            severity="critical",
            location="industries chip grid",
            claim=f"only {len(brief.industries)} industries rendered",
            issue=(
                "Industries grid has fewer than 3 chips. Either the ledger "
                "does not support a meaningful breakdown (render INSUFFICIENT "
                "DATA) or the Author dropped chips the ledger does support."
            ),
            suggested_fix=(
                "If the ledger has ≥3 named industries, cite them; otherwise "
                "drop the section entirely so the page does not look thin."
            ),
        ))
    if len(brief.geographic_footprint) < 1:
        issues.append(ReviewIssue(
            severity="critical",
            location="geographic_footprint",
            claim="empty geography section",
            issue=(
                "Geographic footprint is empty. A research brief without any "
                "evidenced location is structurally incomplete."
            ),
            suggested_fix=(
                "If the ledger has ANY explicit location (city, country, "
                "headquarters), cite it. If not, the Researcher needs to "
                "find one — request supplemental round."
            ),
        ))

    # 2. Tier-1 minimum per required category
    t1_by_cat = tier1_count_per_category(ledger)
    weak_cats: list[str] = []
    for cat in _REQUIRED_BRIEF_CATEGORIES:
        if t1_by_cat.get(cat.value, 0) < _TIER1_MIN_PER_CATEGORY:
            weak_cats.append(cat.value)
    if weak_cats:
        issues.append(ReviewIssue(
            severity="critical",
            location="ledger Tier-1 coverage",
            claim=(
                f"required categories under {_TIER1_MIN_PER_CATEGORY} Tier-1 "
                f"sources: {', '.join(weak_cats)}"
            ),
            issue=(
                "The brief leans on Tier-2/3 sources (aggregators, "
                "self-reported) for these categories. Reach for primary "
                "sources: press articles, SEC filings, gov registries, "
                "conference talks."
            ),
            suggested_fix=(
                "Run a supplemental research round explicitly targeting "
                "Bloomberg, Reuters, NYT, regional business press, "
                "SEC EDGAR, and the entity's official site for each "
                "weak category."
            ),
        ))

    # 3. Aggregator dominance
    agg_share = aggregator_share(ledger)
    if agg_share > 0.30:
        issues.append(ReviewIssue(
            severity="critical",
            location="ledger source mix",
            claim=f"aggregator share {agg_share*100:.0f}%",
            issue=(
                f"More than 30% of verified evidence comes from aggregator "
                f"hosts (theorg, rocketreach, contactout, zoominfo, etc.). "
                f"These rehash LinkedIn data and are not primary sources."
            ),
            suggested_fix=(
                "Researcher must prioritize Tier-1 press, SEC, gov, and "
                "the entity's own official site in the next round."
            ),
        ))

    # 4. Big-badge MEDIUM heuristic estimate (Stripe-class)
    badge = brief.hero.badge if brief.hero else None
    if badge and badge.value and badge.method_id and badge.confidence:
        heuristic_methods = ("saas_revenue_v1", "consulting_firm_revenue_v1")
        if badge.method_id in heuristic_methods:
            high = _parse_high(badge.value)
            if (high is not None
                    and high >= _RIGOR_BIG_BADGE_USD
                    and badge.confidence.value in ("low", "medium")):
                issues.append(ReviewIssue(
                    severity="critical",
                    location="hero badge",
                    claim=f"{badge.value} via {badge.method_id} ({badge.confidence.value})",
                    issue=(
                        f"A {badge.confidence.value}-confidence indirect-signal "
                        f"estimate with upper bound ≥$1B is structurally "
                        f"unreliable. {badge.method_id} is a heuristic recipe "
                        f"not built for this scale."
                    ),
                    suggested_fix=(
                        "Replace with public_disclosure_v1 if a disclosed "
                        "figure is in the ledger, or render INSUFFICIENT "
                        "DATA. Do not present this number."
                    ),
                ))

    return issues


_BADGE_NUM = re.compile(r"\$([\d.]+)\s*([KMB])?", re.IGNORECASE) if False else None
# Re-create cleanly above the function:
import re as _re_for_badge
_BADGE_NUM_RE = _re_for_badge.compile(r"\$([\d.]+)\s*([KMB])?", _re_for_badge.IGNORECASE)


def _parse_high(value_range: str) -> float | None:
    matches = _BADGE_NUM_RE.findall(value_range)
    if len(matches) < 1:
        return None
    # Take the LAST numeric match as the upper bound
    amount, unit = matches[-1]
    try:
        v = float(amount)
    except ValueError:
        return None
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get((unit or "").upper(), 1.0)
    return v * mult


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------


def _extract_pdf_text(pdf_path: Path) -> str:
    """Return concatenated text from every page, with page markers."""
    text, _ = _extract_pdf_text_with_fallback(pdf_path, ctx=None)
    return text


def _extract_pdf_text_with_fallback(
    pdf_path: Path, ctx: PipelineContext | None,
) -> tuple[str, list[int]]:
    """Extract text using pdfplumber. For any page returning <50 chars, try
    pdfminer.six as a second pass. Returns (joined_text, low_text_page_nums)."""
    out: list[str] = []
    low_text_pages: list[int] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            if len(text) < _PDFPLUMBER_MIN_CHARS:
                # E2: pdfminer.six fallback
                fallback = _extract_via_pdfminer(pdf_path, page_index=i - 1)
                if fallback and len(fallback.strip()) > len(text):
                    text = fallback.strip()
                else:
                    low_text_pages.append(i)
                    if ctx is not None:
                        ctx.logger.info(
                            "Reviewer: pdfminer fallback also empty for page %d", i,
                        )
            out.append(f"--- PAGE {i} ---\n{text}")
    return "\n\n".join(out), low_text_pages


def _extract_via_pdfminer(pdf_path: Path, *, page_index: int) -> str:
    """Try pdfminer.six for a single page. Returns "" on any error."""
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        return extract_text(str(pdf_path), page_numbers=[page_index]) or ""
    except Exception:
        return ""


def _rasterize_pdf(pdf_path: Path) -> Iterable[tuple[str, str]]:
    """Yield (base64_png, mime) for each page. Requires poppler at runtime."""
    from pdf2image import convert_from_path
    import io
    images = convert_from_path(str(pdf_path), dpi=120)
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        yield base64.b64encode(buf.getvalue()).decode("ascii"), "image/png"
