"""BriefData — the structured output of Author and the input to Designer.

Mirrors SPEC §4.5 and the visual sections preserved in CLAUDE.md.

Every text-bearing field carries at least one `evidence_id` (or a `method_id`
for estimate-backed values). Empty sections are intentional — Designer omits
them rather than padding (CLAUDE.md rule 2).
"""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from account_research.schemas.evidence import ConfidenceLevel
from account_research.schemas.entity import EntityType


# ---------------------------------------------------------------------------
# Page 1 — hero, quick-take, stats, timeline, who-they-are, DNA cards
# ---------------------------------------------------------------------------


class HeroBadge(BaseModel):
    """Financial badge on page 1. `value` is None when no estimate qualifies —
    Designer renders 'INSUFFICIENT DATA' in that case."""

    model_config = ConfigDict(extra="forbid")

    label: str  # e.g. "EST. REVENUE", "EST. NET WORTH"
    value: str | None  # e.g. "$0.5-2M" or None
    unit: str | None = None  # e.g. "USD/yr"
    caveat: str | None = None
    method_id: str | None = None
    confidence: ConfidenceLevel | None = None


class HeroSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    entity_type: EntityType
    tagline: str | None = None
    badge: HeroBadge | None = None


class QuickTake(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=600)
    best_angle: str | None = Field(default=None, max_length=300)
    evidence_ids: list[UUID] = Field(default_factory=list)


class ContactItem(BaseModel):
    """One outreach contact rendered on page 1 below the quick-take.

    Contacts are factual records (name, title, email, phone) sourced from a
    B2B data provider (currently Apollo). They are NOT routed through the
    evidence ledger / Fact-Checker, because PII rarely appears verbatim on
    fetchable public pages — the Fact-Checker's substring match would
    always flag them as unverifiable. Instead each contact carries its own
    provenance fields. The Designer renders a small "via <source> · verify
    before outreach" caveat under the section.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    linkedin_url: HttpUrl | None = None
    # Provenance — baked in instead of routed through the evidence ledger.
    source: str = Field(default="apollo", max_length=40)
    source_url: HttpUrl | None = None


class StatBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1)
    label: str = Field(min_length=1)
    evidence_id: UUID | None = None  # None only when value is "INSUFFICIENT DATA"


class TimelineMilestone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    description: str | None = None
    start_year: int
    end_year: int | None = None
    color_hint: str | None = None
    evidence_ids: list[UUID] = Field(default_factory=list)


class WhoTheyAreCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str = Field(max_length=60)
    body: str = Field(max_length=220)
    evidence_ids: list[UUID] = Field(default_factory=list)


class DnaCard(BaseModel):
    """Personal-DNA (person briefs) or Company-DNA (company briefs)."""

    model_config = ConfigDict(extra="forbid")

    trait: str = Field(max_length=60)
    detail: str = Field(max_length=220)
    evidence_ids: list[UUID] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Page 2 — industries, geographic footprint, strategic signals
# ---------------------------------------------------------------------------


class IndustryChip(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    evidence_ids: list[UUID] = Field(default_factory=list, min_length=1)


class GeographicLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: str
    is_hq: bool = False
    project_count: int | None = None  # Only if explicitly cited in the ledger
    evidence_ids: list[UUID] = Field(min_length=1)


class StrategicSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str = Field(max_length=60)
    body: str = Field(max_length=220)
    evidence_ids: list[UUID] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Page 3 — engagement readiness, key signals, recommended approach, discovery
# ---------------------------------------------------------------------------


class ScorecardRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(max_length=40)
    score: int = Field(ge=1, le=5)
    rationale: str = Field(max_length=180)
    evidence_ids: list[UUID] = Field(default_factory=list)


class KeySignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(max_length=60)
    body: str = Field(max_length=180)
    evidence_ids: list[UUID] = Field(default_factory=list)


class ApproachItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str = Field(max_length=60)
    body: str = Field(max_length=220)
    evidence_ids: list[UUID] = Field(default_factory=list)


class DiscoveryQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(max_length=200)
    rationale: str | None = Field(default=None, max_length=200)
    evidence_ids: list[UUID] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Page 4 — recap, next steps, sources, methodology, confidence report
# ---------------------------------------------------------------------------


class RecapStat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    label: str
    evidence_id: UUID | None = None


class NextStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(max_length=220)
    evidence_ids: list[UUID] = Field(default_factory=list)


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: HttpUrl
    source_type: str
    evidence_ids: list[UUID] = Field(min_length=1)


class MethodologyNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method_id: str
    summary: str


class ConfidenceReport(BaseModel):
    """Footer line on page 4."""

    model_config = ConfigDict(extra="forbid")

    total_facts: int = Field(ge=0)
    verified: int = Field(ge=0)
    estimated: int = Field(ge=0)
    unverifiable: int = Field(ge=0)
    source_dead: int = Field(ge=0, default=0)


# ---------------------------------------------------------------------------
# Top-level BriefData
# ---------------------------------------------------------------------------


class BriefData(BaseModel):
    """Complete brief payload. Designer renders this as the 4-page PDF."""

    model_config = ConfigDict(extra="forbid")

    # max_length caps come from the 4-page visual budget. The Designer omits
    # rather than pads under-cap, so going below is fine; going over breaks
    # the page count promise. Author prompt mirrors these limits.
    entity_id: UUID
    hero: HeroSection
    quick_take: QuickTake
    contacts: list[ContactItem] = Field(default_factory=list, max_length=3)
    stats: list[StatBlock] = Field(default_factory=list, max_length=4)
    timeline: list[TimelineMilestone] = Field(default_factory=list, max_length=6)
    who_they_are_cards: list[WhoTheyAreCard] = Field(default_factory=list, max_length=4)
    dna_cards: list[DnaCard] = Field(default_factory=list, max_length=4)
    industries: list[IndustryChip] = Field(default_factory=list, max_length=8)
    geographic_footprint: list[GeographicLocation] = Field(default_factory=list, max_length=6)
    strategic_signals: list[StrategicSignal] = Field(default_factory=list, max_length=4)
    scorecard: list[ScorecardRow] = Field(default_factory=list, max_length=6)
    key_signals: list[KeySignal] = Field(default_factory=list, max_length=4)
    recommended_approach: list[ApproachItem] = Field(default_factory=list, max_length=3)
    discovery_questions: list[DiscoveryQuestion] = Field(default_factory=list, max_length=3)
    recap_stats: list[RecapStat] = Field(default_factory=list, max_length=4)
    next_steps: list[NextStep] = Field(default_factory=list, max_length=2)
    sources: list[SourceRef] = Field(default_factory=list, max_length=8)
    methodology: list[MethodologyNote] = Field(default_factory=list, max_length=4)
    confidence_report: ConfidenceReport | None = None

    @classmethod
    def skeleton(
        cls,
        entity,  # account_research.schemas.entity.Entity (avoid circular import)
        ledger,  # list[VerifiedEvidenceItem]
        estimates,  # list[Estimate]
    ) -> "BriefData":
        """Minimal valid BriefData used as a fallback when the Author crashes.

        Renders to a one-page PDF with just the hero (entity name + estimate
        badge if available) and an "Insufficient public data" quick-take.
        The Designer's graceful-omission logic handles every other section.

        This guarantees the pipeline rule from memory: a PDF is ALWAYS
        produced, however thin. The downstream confidence score reflects
        the degraded state — that's what the score is for.
        """
        badge: HeroBadge | None = None
        if estimates:
            e = estimates[0]
            badge = HeroBadge(
                label=f"EST. {e.metric.upper()}"[:40],
                value=e.value_range,
                unit=e.unit,
                caveat=e.caveat_text,
                method_id=e.method_id,
                confidence=e.confidence,
            )

        body = (
            "Insufficient public data. The Author agent could not produce a "
            "structured brief — see the evidence ledger panel for the raw "
            "sources collected by the Researcher. Re-run or refine to retry."
        )

        verified = sum(1 for i in ledger if getattr(i.verification, "status", None) == "verified")
        unverifiable = sum(1 for i in ledger if getattr(i.verification, "status", None) == "unverifiable")
        dead = sum(1 for i in ledger if getattr(i.verification, "status", None) == "source_dead")

        return cls(
            entity_id=entity.id,
            hero=HeroSection(
                name=entity.name,
                entity_type=entity.type,
                tagline=None,
                badge=badge,
            ),
            quick_take=QuickTake(
                body=body,
                best_angle=None,
                evidence_ids=[],
            ),
            confidence_report=ConfidenceReport(
                total_facts=len(ledger),
                verified=verified,
                estimated=len(estimates),
                unverifiable=unverifiable,
                source_dead=dead,
            ),
        )

    def all_evidence_ids(self) -> set[UUID]:
        """Walk every section and return the union of cited evidence_ids.

        Used by Author's post-hoc traceability check and by Reviewer."""
        ids: set[UUID] = set()
        for ev_list in (
            self.quick_take.evidence_ids,
            *[s.evidence_ids for s in self.industries],
            *[g.evidence_ids for g in self.geographic_footprint],
            *[w.evidence_ids for w in self.who_they_are_cards],
            *[d.evidence_ids for d in self.dna_cards],
            *[t.evidence_ids for t in self.timeline],
            *[sig.evidence_ids for sig in self.strategic_signals],
            *[sc.evidence_ids for sc in self.scorecard],
            *[ks.evidence_ids for ks in self.key_signals],
            *[a.evidence_ids for a in self.recommended_approach],
            *[q.evidence_ids for q in self.discovery_questions],
            *[ns.evidence_ids for ns in self.next_steps],
            *[src.evidence_ids for src in self.sources],
        ):
            ids.update(ev_list)
        for stat in self.stats:
            if stat.evidence_id is not None:
                ids.add(stat.evidence_id)
        for stat in self.recap_stats:
            if stat.evidence_id is not None:
                ids.add(stat.evidence_id)
        return ids
