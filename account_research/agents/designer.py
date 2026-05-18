"""Designer — render BriefData to PDF.

SPEC §4.6. No LLM. Thin wrapper around the parameterized pdf_builder that
counts what was actually rendered and surfaces warnings for sparse sections.
The Reviewer downstream consumes the rendered PDF + this report.

A3: when a supporting ledger is supplied, the Designer pre-validates that
every ``evidence_id`` cited in the brief actually exists in the ledger.
Orphans raise :class:`OrphanEvidenceError` BEFORE the PDF is written —
fail-fast at the cheapest possible step instead of paying an Author +
Reviewer iteration to detect a buggy brief.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from account_research.agents.base import (
    BaseAgent,
    OrphanEvidenceError,
    PipelineContext,
)
from account_research.designer.pdf_builder import build_brief
from account_research.schemas.brief import BriefData
from account_research.schemas.evidence import VerifiedEvidenceItem


class DesignInput(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    brief: BriefData
    out_path: str
    # Optional: when provided, Designer pre-validates that every cited
    # evidence_id in the brief exists in this ledger. Omit for the
    # ``cli design`` subcommand where the brief is hand-loaded from disk.
    ledger: list[VerifiedEvidenceItem] = Field(default_factory=list)


class SectionAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    rendered: bool
    item_count: int = 0


class DesignReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "layout_issue"] = "ok"
    pdf_path: str
    page_count: int = 4  # template is fixed at 4 pages
    sections: list[SectionAudit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    badge_rendered: bool = False
    badge_was_null: bool = False

    @property
    def rendered_section_count(self) -> int:
        return sum(1 for s in self.sections if s.rendered)


class DesignerAgent(BaseAgent[DesignInput, DesignReport]):
    name = "designer"
    model = ""  # No LLM
    input_schema = DesignInput
    output_schema = DesignReport

    def run(self, payload: DesignInput, ctx: PipelineContext) -> DesignReport:
        brief = payload.brief
        out_path = Path(payload.out_path)

        # A3: orphan-evidence fail-fast. Author already filters orphans on its
        # way out (see _filter_unknown_citations) — this guard catches the
        # belt-and-suspenders case where a hand-edited brief slips through or
        # Author's filter has a regression. Cheaper to detect here than after
        # a full Reviewer iteration ($5+ in Opus tokens).
        if payload.ledger:
            ledger_ids = {ev.id for ev in payload.ledger}
            orphans = brief.all_evidence_ids() - ledger_ids
            if orphans:
                ctx.logger.warning(
                    "Designer: brief cites %d evidence_id(s) not in the "
                    "ledger — aborting render so orchestrator can revise",
                    len(orphans),
                )
                raise OrphanEvidenceError(orphans)

        path = build_brief(brief, out_path)
        ctx.logger.info("Designer rendered PDF to %s", path)

        # Audit each section: was it present in the input?
        sections = [
            SectionAudit(name="hero", rendered=True, item_count=1),
            SectionAudit(name="quick_take", rendered=bool(brief.quick_take.body), item_count=1),
            SectionAudit(name="stats", rendered=bool(brief.stats), item_count=len(brief.stats)),
            SectionAudit(name="timeline", rendered=bool(brief.timeline), item_count=len(brief.timeline)),
            SectionAudit(name="who_they_are", rendered=bool(brief.who_they_are_cards),
                         item_count=len(brief.who_they_are_cards)),
            SectionAudit(name="dna", rendered=bool(brief.dna_cards), item_count=len(brief.dna_cards)),
            SectionAudit(name="industries", rendered=bool(brief.industries),
                         item_count=len(brief.industries)),
            SectionAudit(name="geographic_footprint", rendered=bool(brief.geographic_footprint),
                         item_count=len(brief.geographic_footprint)),
            SectionAudit(name="strategic_signals", rendered=bool(brief.strategic_signals),
                         item_count=len(brief.strategic_signals)),
            SectionAudit(name="scorecard", rendered=bool(brief.scorecard),
                         item_count=len(brief.scorecard)),
            SectionAudit(name="key_signals", rendered=bool(brief.key_signals),
                         item_count=len(brief.key_signals)),
            SectionAudit(name="recommended_approach", rendered=bool(brief.recommended_approach),
                         item_count=len(brief.recommended_approach)),
            SectionAudit(name="discovery_questions", rendered=bool(brief.discovery_questions),
                         item_count=len(brief.discovery_questions)),
            SectionAudit(name="recap_stats", rendered=bool(brief.recap_stats),
                         item_count=len(brief.recap_stats)),
            SectionAudit(name="next_steps", rendered=bool(brief.next_steps),
                         item_count=len(brief.next_steps)),
            SectionAudit(name="sources", rendered=bool(brief.sources), item_count=len(brief.sources)),
            SectionAudit(name="methodology",
                         rendered=bool(brief.methodology) or brief.confidence_report is not None,
                         item_count=len(brief.methodology)),
        ]

        badge = brief.hero.badge
        badge_was_null = badge is None or badge.value is None
        badge_rendered = True  # we always render some badge, even "INSUFFICIENT DATA"

        warnings: list[str] = []
        if badge_was_null:
            warnings.append("Hero badge rendered as 'INSUFFICIENT DATA' — no estimate available.")
        if not brief.sources:
            warnings.append("No sources cited — page 4 sources grid is omitted.")
        rendered_count = sum(1 for s in sections if s.rendered)
        if rendered_count < 8:
            warnings.append(
                f"Only {rendered_count}/{len(sections)} sections present — "
                "pages will look sparse. Consider re-running Researcher."
            )

        return DesignReport(
            pdf_path=str(path),
            sections=sections,
            warnings=warnings,
            badge_rendered=badge_rendered,
            badge_was_null=badge_was_null,
        )
