"""Refine — incremental upgrade of an existing brief without re-running the
full pipeline.

The user clicks "Refine" on a brief whose confidence is too low. Instead of
starting from scratch (and paying the full Disambiguator + Researcher first
pass again), refine:

  1. Loads the existing entity + verified ledger from the DB.
  2. Computes a `WeaknessProfile`: required-category coverage gaps, source
     tier mix (Tier-1 share), dead-link count, unverifiable count.
  3. Runs ONE supplemental Researcher round focused on the gaps + tier
     weaknesses (uses the existing `_supplemental_prompt` machinery).
  4. Re-fact-checks ONLY the new items (and optionally retries the JS
     fallback on previously source_dead items).
  5. Re-runs the existing Estimator → Author → Designer → Reviewer chain on
     the AUGMENTED ledger.

Cost: roughly 30-50% of a full run because Disambiguator + first Researcher
batch + first Fact-Check sweep are reused as-is.

This module is intentionally pure logic — no LLM calls, no DB writes, no
side effects. The CLI orchestrates the actual pipeline; this file just
exposes the analysis + prompt builders that the CLI consumes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from account_research.agents.source_authority import (
    aggregator_share,
    tier_breakdown,
    tier_of,
)
from account_research.schemas.entity import Entity
from account_research.schemas.evidence import (
    EvidenceCategory,
    VerifiedEvidenceItem,
)


# Required categories the brief needs at least one item from to render
# meaningfully. Mirrors REQUIRED_CATEGORIES in researcher.py.
REQUIRED_CATEGORIES = [
    EvidenceCategory.COMPANY_FACTS,
    EvidenceCategory.FINANCIAL,
    EvidenceCategory.LEADERSHIP,
    EvidenceCategory.PRODUCTS,
    EvidenceCategory.CLIENTS,
    EvidenceCategory.GEOGRAPHY,
]

# Below this Tier-1 share the brief leans too heavily on aggregators /
# Tier-2 sources; refine should hunt for primary sources.
LOW_TIER1_SHARE_THRESHOLD = 0.30

# Above this aggregator share we want to push for primary sources too.
HIGH_AGGREGATOR_SHARE_THRESHOLD = 0.40

# Below this verification rate the ledger is mostly junk and refine should
# focus on re-fetching dead/unverifiable items.
LOW_VERIFICATION_RATE_THRESHOLD = 0.70


@dataclass
class WeaknessProfile:
    """What's wrong with the current ledger, in machine-readable form."""

    category_gaps: list[EvidenceCategory] = field(default_factory=list)
    """Required categories with zero verified items."""

    tier_counts: dict[int, int] = field(default_factory=dict)
    """{1: count_tier1, 2: count_tier2, 3: count_tier3} on verified items."""

    tier1_share: float = 0.0
    """Fraction of verified items from Tier-1 sources."""

    aggregator_share: float = 0.0
    """Fraction of verified items from aggregators (Tier-3)."""

    verification_rate: float = 0.0
    """verified / total in the supplied ledger."""

    dead_link_count: int = 0
    unverifiable_count: int = 0
    verified_count: int = 0
    total_count: int = 0

    def is_thin(self) -> bool:
        """Whether the ledger has any signal worth refining at all."""
        return self.verified_count == 0

    def needs_tier1_push(self) -> bool:
        return (
            self.tier1_share < LOW_TIER1_SHARE_THRESHOLD
            or self.aggregator_share > HIGH_AGGREGATOR_SHARE_THRESHOLD
        )

    def needs_link_refresh(self) -> bool:
        return self.dead_link_count > 0 or self.unverifiable_count >= 3

    def summary_line(self) -> str:
        """One-liner suitable for logging."""
        return (
            f"verified={self.verified_count}/{self.total_count} "
            f"({self.verification_rate:.0%}), "
            f"T1={self.tier_counts.get(1, 0)} "
            f"T2={self.tier_counts.get(2, 0)} "
            f"T3={self.tier_counts.get(3, 0)} "
            f"(T1 share={self.tier1_share:.0%}, aggregator={self.aggregator_share:.0%}), "
            f"dead={self.dead_link_count}, "
            f"unverifiable={self.unverifiable_count}, "
            f"gaps={[c.value for c in self.category_gaps]}"
        )


def analyze_weaknesses(
    items: list[VerifiedEvidenceItem],
) -> WeaknessProfile:
    """Compute the weakness profile from an existing verified ledger.

    `items` is the full ledger as persisted (verified + unverifiable + dead).
    """
    total = len(items)
    verified = [i for i in items if i.verification.status == "verified"]
    unverifiable = [i for i in items if i.verification.status == "unverifiable"]
    dead = [i for i in items if i.verification.status == "source_dead"]

    present_cats = {i.category for i in verified}
    gaps = [c for c in REQUIRED_CATEGORIES if c not in present_cats]

    tiers = tier_breakdown(items)
    tier_total = sum(tiers.values())
    t1_share = (tiers.get(1, 0) / tier_total) if tier_total else 0.0
    agg_share = aggregator_share(items)

    verification_rate = (len(verified) / total) if total else 0.0

    return WeaknessProfile(
        category_gaps=gaps,
        tier_counts=tiers,
        tier1_share=t1_share,
        aggregator_share=agg_share,
        verification_rate=verification_rate,
        dead_link_count=len(dead),
        unverifiable_count=len(unverifiable),
        verified_count=len(verified),
        total_count=total,
    )


# ---------------------------------------------------------------------------
# Supplemental research prompt builder
# ---------------------------------------------------------------------------


_CATEGORY_HINTS: dict[EvidenceCategory, str] = {
    EvidenceCategory.COMPANY_FACTS: (
        "founding year, legal entity, headquarters, ownership "
        "(official site About page, gov registries, Wikipedia, Crunchbase)"
    ),
    EvidenceCategory.FINANCIAL: (
        "revenue, funding rounds, valuation, profitability "
        "(SEC filings, Crunchbase, PitchBook, press releases, 10-K)"
    ),
    EvidenceCategory.LEADERSHIP: (
        "founder, CEO, key executives with verbatim titles "
        "(About / Team / Leadership page, LinkedIn, press)"
    ),
    EvidenceCategory.PRODUCTS: (
        "core products / services, market positioning, recent launches "
        "(official site Products page, press, blog, App Store)"
    ),
    EvidenceCategory.CLIENTS: (
        "named customers, case studies, partnerships "
        "(case-studies page, customer logos, press)"
    ),
    EvidenceCategory.GEOGRAPHY: (
        "offices, countries served, regional presence "
        "(Locations page, footer, Wikipedia)"
    ),
}


def build_focus_prompt(
    entity: Entity,
    profile: WeaknessProfile,
    existing_evidence_ids: list[str],
    focus_category: EvidenceCategory | None = None,
) -> str:
    """Build the user message for a supplemental Researcher round.

    The prompt tells the model:
      - the entity context
      - what we already have (so it doesn't re-emit duplicates)
      - what's weak (categories, tier mix, dead links)
      - what to prioritize (Tier-1 sources)
      - optional user-supplied focus_category override

    Returns a single string ready to drop into `messages[0].content`.
    """
    primary = str(entity.primary_url) if entity.primary_url else "(no primary URL)"
    aliases = ", ".join(getattr(entity, "aliases", None) or []) or "(none)"

    if focus_category is not None:
        target_categories = [focus_category]
        focus_note = (
            f"\nUser-directed focus: prioritise this category over others.\n"
        )
    else:
        target_categories = profile.category_gaps or list(REQUIRED_CATEGORIES)
        focus_note = ""

    cat_block = "\n".join(
        f"  - {c.value}: {_CATEGORY_HINTS.get(c, '(no hints)')}"
        for c in target_categories
    )

    tier_block = (
        f"  current verified count: {profile.verified_count} / {profile.total_count} "
        f"({profile.verification_rate:.0%} verification rate)\n"
        f"  current tier mix: T1={profile.tier_counts.get(1, 0)} "
        f"T2={profile.tier_counts.get(2, 0)} "
        f"T3={profile.tier_counts.get(3, 0)} "
        f"(T1 share={profile.tier1_share:.0%}, "
        f"aggregator share={profile.aggregator_share:.0%})"
    )

    tier_directive = ""
    if profile.needs_tier1_push():
        tier_directive = (
            "\nTIER-1 PUSH: the existing ledger leans on aggregators / "
            "Tier-2 sources. Prefer primary sources for new items: official "
            "company site (About, Press, Investors pages), SEC filings / "
            "10-Ks, government registries, top-tier business press (FT, "
            "WSJ, Bloomberg, Reuters, Forbes). Cross-corroborate every "
            "Tier-3 claim with at least one Tier-1 if possible.\n"
        )

    link_directive = ""
    if profile.needs_link_refresh():
        link_directive = (
            f"\nLINK REFRESH: {profile.dead_link_count} source(s) returned "
            f"dead and {profile.unverifiable_count} were unverifiable. When "
            f"the same claim has a fresh URL elsewhere, prefer the fresh one.\n"
        )

    known_ids_preview = (
        "\n".join(f"  - {i}" for i in existing_evidence_ids[:30])
        + (f"\n  ... ({len(existing_evidence_ids) - 30} more)"
           if len(existing_evidence_ids) > 30 else "")
    )

    return (
        f"REFINE MODE — supplemental research round.\n\n"
        f"Entity:\n"
        f"  entity_id: {entity.id}\n"
        f"  name: {entity.name}\n"
        f"  type: {entity.type.value}\n"
        f"  primary_url: {primary}\n"
        f"  aliases: {aliases}\n\n"
        f"Current ledger state:\n{tier_block}\n\n"
        f"Categories to fill or strengthen:\n{cat_block}\n"
        f"{focus_note}"
        f"{tier_directive}"
        f"{link_directive}"
        f"\nDO NOT re-emit items you already produced previously. Existing "
        f"evidence_ids in the ledger (do not duplicate by url + claim):\n"
        f"{known_ids_preview}\n\n"
        f"Output a SUPPLEMENTAL EvidenceBatch with the new items only. "
        f"Same rules: verbatim raw_quote, no fabrication, no padding."
    )
