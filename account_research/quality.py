"""Brief-quality metrics over a BriefData + its supporting ledger.

These describe the BRIEF (what shipped in the PDF), not the raw Researcher
output. They complement the Fact-Checker's `verification_rate`, which is a
LEDGER metric — useful for diagnosing source selection, but not a direct
measure of how trustworthy the rendered PDF is.

Terminology surfaced in the UI:
  - Sourcing rate: ledger metric (verified items / total items). Diagnostic.
  - Citation backing: brief metric — % of cited evidence_ids that are verified.
  - Tier-1 share (cited): % of cited items from primary sources.
  - Sections rendered: PDF-completeness ratio.
  - Confidence score: composite 0-100 blending the above.
"""
from __future__ import annotations

from typing import Iterable
from uuid import UUID

from account_research.agents.source_authority import tier_of
from account_research.schemas.brief import BriefData
from account_research.schemas.evidence import SourceType, VerifiedEvidenceItem


# All BriefData top-level fields that render as a distinct PDF section.
# `hero` and `quick_take` are always populated (model contract); the rest
# are populated iff non-empty / non-None.
_LIST_SECTIONS = (
    "stats", "timeline", "who_they_are_cards", "dna_cards", "industries",
    "geographic_footprint", "strategic_signals", "scorecard",
    "key_signals", "recommended_approach", "discovery_questions",
    "recap_stats", "next_steps", "sources", "methodology",
)
_SECTIONS_TOTAL = 2 + len(_LIST_SECTIONS)  # hero + quick_take + list sections


def _by_id(ledger: list[VerifiedEvidenceItem]) -> dict[UUID, VerifiedEvidenceItem]:
    return {ev.id: ev for ev in ledger}


def citation_backing(
    brief: BriefData, ledger: list[VerifiedEvidenceItem]
) -> float:
    """% of cited evidence_ids whose verification.status is 'verified'.

    Evidence_ids cited by the brief but missing from the ledger are counted
    as NOT verified (defensive — should never happen post UUID validator).
    A brief with zero citations returns 100.0 (vacuously fully-backed; the
    Designer will have rendered 'Insufficient public data' for empty fields).
    """
    cited = brief.all_evidence_ids()
    if not cited:
        return 100.0
    lookup = _by_id(ledger)
    verified = 0
    for eid in cited:
        ev = lookup.get(eid)
        if ev is not None and ev.verification.status == "verified":
            verified += 1
    return round(100.0 * verified / len(cited), 1)


def tier1_share_among_cited(
    brief: BriefData, ledger: list[VerifiedEvidenceItem]
) -> float:
    """% of cited evidence items whose source is Tier-1.

    Tier-1 = official site / SEC / gov registry / mainstream business press,
    per `account_research.agents.source_authority.tier_of`. Aggregator hosts
    (Tier-3) drag this down.
    """
    cited = brief.all_evidence_ids()
    if not cited:
        return 100.0
    lookup = _by_id(ledger)
    tier1 = 0
    counted = 0
    for eid in cited:
        ev = lookup.get(eid)
        if ev is None:
            continue
        counted += 1
        if tier_of(str(ev.source_url), ev.source_type) == 1:
            tier1 += 1
    if counted == 0:
        return 100.0
    return round(100.0 * tier1 / counted, 1)


def sections_rendered(brief: BriefData) -> tuple[int, int]:
    """Return (populated_count, total_count). hero + quick_take always count;
    list-sections only when non-empty."""
    populated = 2  # hero + quick_take
    for attr in _LIST_SECTIONS:
        val = getattr(brief, attr, None)
        if val:
            populated += 1
    return populated, _SECTIONS_TOTAL


def _badge_caveat_score(brief: BriefData) -> float:
    """1.0 when the badge either has no estimate OR has a non-empty caveat.
    0.0 when it has a method_id-backed value but no visible caveat."""
    badge = brief.hero.badge
    if badge is None or badge.method_id is None or badge.value is None:
        return 1.0  # nothing to caveat
    return 1.0 if (badge.caveat or "").strip() else 0.0


def confidence_score(
    brief: BriefData, ledger: list[VerifiedEvidenceItem]
) -> float:
    """Composite 0-100 over four dimensions:

      50% — citation backing (verified-status of cited evidence)
      25% — sections rendered (PDF completeness)
      15% — Tier-1 share among cited (source authority)
      10% — badge caveat present (no bare estimates)

    Tunable; the weights reflect "no claim without a verified citation" being
    the dominant rule, while completeness and source authority round it out.
    """
    backing = citation_backing(brief, ledger)
    tier1 = tier1_share_among_cited(brief, ledger)
    populated, total = sections_rendered(brief)
    sections_pct = 100.0 * populated / total
    caveat_pct = 100.0 * _badge_caveat_score(brief)

    score = (
        0.50 * backing
        + 0.25 * sections_pct
        + 0.15 * tier1
        + 0.10 * caveat_pct
    )
    return round(score, 1)


def ledger_only_score(rows: Iterable) -> float | None:
    """Fallback 0-100 confidence for entities without a (loadable) brief.

    Computed from SQLAlchemy `EvidenceRow`s alone — uses `verification_status`
    and `source_url`/`source_type` directly so we don't depend on the Pydantic
    brief. Returns None for an empty ledger (caller renders "—").

      60% — verified rate over CHECKABLE items (verified+unverifiable;
            excludes source_dead so URL rot doesn't tank the score)
      30% — Tier-1 share (source_authority.tier_of == 1) over checkable items
      10% — volume floor: min(checkable, 10) / 10
    """
    rows = list(rows)
    if not rows:
        return None
    # Exclude source_dead from BOTH numerators and denominators: link rot
    # is outside the Researcher's control.
    checkable_rows = [r for r in rows if r.verification_status != "source_dead"]
    checkable = len(checkable_rows)
    if checkable == 0:
        # Every item is source_dead — there's nothing to score, but a
        # ledger exists. Return 0 instead of None so callers don't render "—".
        return 0.0
    verified = sum(1 for r in checkable_rows if r.verification_status == "verified")
    tier1 = 0
    for r in checkable_rows:
        try:
            st = SourceType(r.source_type)
        except ValueError:
            st = None
        if tier_of(r.source_url, st) == 1:
            tier1 += 1
    verified_rate = verified / checkable
    tier1_share = tier1 / checkable
    volume = min(checkable, 10) / 10.0
    score = 100.0 * (0.60 * verified_rate + 0.30 * tier1_share + 0.10 * volume)
    return round(score, 1)
