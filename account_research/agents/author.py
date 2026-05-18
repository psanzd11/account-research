"""Author — draft a BriefData payload from a verified ledger + estimates.

SPEC §4.5. Uses Opus 4.7 (the only LLM call in the Sprint-3 pipeline). Every
text-bearing field in the output must cite ≥1 evidence_id (or carry a
method_id for estimate-backed values). The agent post-validates this and
drops citations that don't exist in the input ledger.

Empty sections are honest — the Designer will omit them. The Author never
pads to fill layout.
"""
from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from account_research.agents.base import BaseAgent, PipelineContext
from account_research.agents.citation_validator import compute_weak_citations
from account_research.llm_client import OPUS
from account_research.schemas.brief import BriefData, HeroSection, QuickTake
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.estimate import Estimate
from account_research.schemas.evidence import VerifiedEvidenceItem
from account_research.schemas.review import ReviewIssue


# Feature flag for the Phase 5 semantic citation validator (D2). Default ON.
_SEMANTIC_VALIDATOR_ENABLED = (
    os.environ.get("AUTHOR_SEMANTIC_VALIDATOR", "1").lower()
    not in ("0", "false", "no", "off")
)
# Truncation cap (D1). Raised from 240 → 500 so Author paraphrases from
# fuller context.
_RAW_QUOTE_TRUNC = int(os.environ.get("AUTHOR_RAW_QUOTE_TRUNC", "500"))


SYSTEM_PROMPT = """\
You are the **Author** agent in a citation-grounded research pipeline.

You receive a verified evidence ledger and (optionally) financial estimates
for ONE entity. Your job is to produce a structured BriefData payload that
will render as a 4-page visual brief. Every claim in your output must be
traceable to a specific evidence_id from the ledger you are given, or to a
method_id from the estimates list.

NON-NEGOTIABLE RULES
1. **No claim without a citation.** Every text-bearing field carries one or
   more evidence_ids. For the badge, also carry method_id from the estimate.
2. **No padding.** If only 4 industries are evidenced, emit 4 IndustryChip
   items, not 12. If only 1 country is evidenced, emit 1 GeographicLocation.
   The PDF template adapts to whatever you provide.
3. **No invented numbers.** If a fact is not in the ledger, do NOT emit it.
   Counts in `geographic_footprint[].project_count` must come from explicit
   evidence; if not present, leave them null.
4. **No conflated facts.** If two ledger items present numbers from
   different time periods, treat them as different and prefer the more
   recent / authoritative one.
5. **Estimates carry their caveat.** Hero badge value comes from the
   provided Estimate. Copy its caveat_text VERBATIM into HeroBadge.caveat.
6. **Use only the UUIDs you were given.** evidence_ids in your output must
   be exact copies of ledger item IDs. method_id strings must be exact
   copies of estimate method_id values.
7. **Semantic citation match.** Each evidence_id you cite must contain
   language that DIRECTLY supports the prose. If you write "operates in 5
   countries" the cited quote must literally enumerate ≥5 countries or
   state a count. If only one country is named in the cited quote, do NOT
   cite it as backing for a multi-country claim — find a quote that does,
   or narrow the claim to match the quote. A semantic validator runs after
   you emit, and weak citations are surfaced to the Reviewer for arbitration.

SECTION GUIDE (MAX LENGTHS ARE HARD CAPS — the schema rejects overshoots)
- hero.entity_type: copy from the entity input.
- hero.tagline: a single-line summary derived from ledger claims (1-2 cited).
- hero.badge: pick the most relevant estimate for the entity type.
  - company → revenue estimate (consulting_firm_revenue_v1 or saas_revenue_v1)
  - person  → net_worth_individual_v1
  - If no estimate, set badge.value=null with no caveat — the Designer
    renders "INSUFFICIENT DATA".
- quick_take: 80-160 words; carry evidence_ids of every sourced statement.
- stats: 2-4 cards (max 4). Each value must come from explicit evidence.
- timeline: 3-6 milestones if the ledger supports them; else empty list (max 6).
- who_they_are_cards: 2-4 cards (max 4).
- dna_cards: 2-4 cards (max 4).
- industries: only those explicitly named in the ledger (max 8).
- geographic_footprint: only locations explicitly cited (max 6). is_hq when
  stated. project_count only when an explicit per-location count was cited.
- strategic_signals: 2-4 cards summarizing posture (max 4).
- scorecard: 4-6 rows (max 6) scoring Reachability, Decision Speed,
  Partnership Openness, Buying Power, Reseller Potential, Scale Headroom
  (skip rows for which no evidence exists). Score 1-5.
- key_signals: 2-4 short callouts (max 4) — positive/caution/discovery-gap.
- recommended_approach: 1-3 items (max 3) — best entry point, opening hook.
- discovery_questions: 3 questions (max 3) to ask in a first call.
- recap_stats: 3-4 most important stats from page 1 (max 4, re-stated).
- next_steps: 1-2 short prescriptions (max 2).
- sources: one SourceRef per UNIQUE source_url in the ledger (max 8).
  Each cites the evidence items it backs.
- methodology: one MethodologyNote per estimate.method_id used (max 4).
- confidence_report: total / verified / estimated / unverifiable / source_dead.

PAGE BUDGET
The visual template targets 4 pages. The hard caps above keep content within
that budget. If you have more candidate content than a cap allows, PICK the
strongest cited items and DROP the rest. Do not stuff.

TEXT LENGTH CAPS (also hard — the schema rejects overshoots)
- quick_take.body:                ≤600 chars (≈100 words)
- quick_take.best_angle:          ≤300 chars
- who_they_are/dna card body:     ≤220 chars (one tight sentence each)
- strategic_signals body:         ≤220 chars
- recommended_approach body:      ≤220 chars
- scorecard rationale:            ≤180 chars
- key_signals body:               ≤180 chars
- discovery_questions question:   ≤200 chars
- next_steps body:                ≤220 chars

Write tight. The Designer renders each field in a small card or paragraph;
verbose prose wraps awkwardly and forces page overflow. One strong cited
sentence beats three explanatory ones.

WHEN AN ESTIMATE IS MISSING OR INSUFFICIENT
- Set hero.badge.value to null. Do not invent a range.
- Note in next_steps that financial enrichment is required.
- Do not fabricate methodology entries.

OUTPUT
End your turn by calling emit_briefdata exactly once.
"""


class AuthorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    entity: Entity
    ledger: list[VerifiedEvidenceItem]
    estimates: list[Estimate] = Field(default_factory=list)
    # Revision-mode fields — populated by the orchestrator when re-running
    # Author after a Reviewer critique.
    previous_brief: BriefData | None = None
    reviewer_issues: list[ReviewIssue] = Field(default_factory=list)
    previous_weak_citations: list[dict] = Field(default_factory=list)
    iteration: int = 1


class AuthorAgent(BaseAgent[AuthorInput, BriefData]):
    name = "author"
    model = OPUS
    input_schema = AuthorInput
    output_schema = BriefData

    # Populated after run() so the orchestrator can pass the list into the
    # next iteration's AuthorInput.previous_weak_citations and into
    # ReviewInput.weak_citations. Reset on each run().
    last_weak_citations: list[dict] = []

    def run(self, payload: AuthorInput, ctx: PipelineContext) -> BriefData:
        brief = self._emit_brief(payload, ctx)

        # Sprint 1.3 — blocking semantic validator. If the first emission has
        # weak citations, make ONE focused re-author pass; if weak citations
        # remain after that, strip the offending fields rather than ship them.
        weak: list[dict] = []
        if _SEMANTIC_VALIDATOR_ENABLED:
            weak = compute_weak_citations(brief, payload.ledger)
            if weak:
                ctx.logger.info(
                    "Author: %d weak citation(s) flagged on first pass; "
                    "making one blocking re-pass",
                    len(weak),
                )
                synthetic_issues = _weak_to_review_issues(weak)
                repass_payload = payload.model_copy(update={
                    "previous_brief": brief,
                    "reviewer_issues": list(payload.reviewer_issues) + synthetic_issues,
                    "previous_weak_citations": weak,
                    "iteration": payload.iteration + 1,
                })
                brief = self._emit_brief(repass_payload, ctx)
                weak = compute_weak_citations(brief, payload.ledger)
                if weak:
                    ctx.logger.warning(
                        "Author: %d weak citation(s) survived re-pass; "
                        "replacing affected fields with 'Insufficient public data'",
                        len(weak),
                    )
                    brief = _strip_weak_citation_fields(brief, weak)
                    # After stripping, re-compute so callers see the truthful
                    # remaining set (should be empty / unrelated locations only).
                    weak = compute_weak_citations(brief, payload.ledger)
        self.last_weak_citations = weak
        return brief

    def _emit_brief(self, payload: AuthorInput, ctx: PipelineContext) -> BriefData:
        """One Author pass: build prompt, call LLM, post-validate citations.

        Does NOT run the semantic validator — that's the caller's job so it can
        orchestrate a blocking re-pass.
        """
        llm = ctx.require_llm()

        # Filter to items acceptable for citation: Fact-Checker-verified OR
        # Tier-1 source (official site / SEC / gov registry) at high confidence
        # whose re-fetch came back unverifiable (typically JS-rendered pages).
        # See VerifiedEvidenceItem.is_acceptable_for_author for the rule.
        confidence_order = {"verified": 0, "high": 1, "medium": 2, "low": 3, "estimated": 4, "unknown": 5}
        usable = [ev for ev in payload.ledger if ev.is_acceptable_for_author()]
        usable.sort(key=lambda e: confidence_order.get(e.confidence.value, 9))

        verified_count = sum(1 for e in usable if e.verification.status == "verified")
        tier1_fallback_count = len(usable) - verified_count
        if tier1_fallback_count:
            ctx.logger.info(
                "Author: %d verified item(s) + %d Tier-1/high fallback item(s) "
                "accepted into usable set",
                verified_count, tier1_fallback_count,
            )

        if not usable:
            ctx.logger.warning(
                "Author: no acceptable items in ledger; brief will be very sparse"
            )

        # D1: raw_quote truncation lifted from 240 → 500 chars so the model
        # paraphrases from fuller context.
        ledger_payload = [
            {
                "id": str(ev.id),
                "claim": ev.claim,
                "raw_quote": (
                    ev.raw_quote[:_RAW_QUOTE_TRUNC]
                    + ("..." if len(ev.raw_quote) > _RAW_QUOTE_TRUNC else "")
                ),
                "category": ev.category.value,
                "source_url": str(ev.source_url),
                "source_type": ev.source_type.value,
                "confidence": ev.confidence.value,
                "notes": ev.notes,
            }
            for ev in usable
        ]

        estimates_payload = [
            {
                "method_id": e.method_id,
                "metric": e.metric,
                "value_range": e.value_range,
                "unit": e.unit,
                "confidence": e.confidence.value,
                "caveat_text": e.caveat_text,
                "assumptions": e.assumptions,
                "signals_used": [
                    {"evidence_id": str(s.evidence_id), "signal": s.signal}
                    for s in e.signals_used
                ],
            }
            for e in payload.estimates
        ]

        is_revision = payload.previous_brief is not None and bool(payload.reviewer_issues)
        revision_block = ""
        if is_revision:
            issues_json = json.dumps(
                [i.model_dump(mode="json") for i in payload.reviewer_issues],
                ensure_ascii=False, indent=2,
            )
            weak_block = ""
            if payload.previous_weak_citations:
                weak_json = json.dumps(
                    payload.previous_weak_citations, ensure_ascii=False, indent=2,
                )
                weak_block = (
                    "\nThe semantic validator also flagged the following citations "
                    "in your previous output as weakly grounded — the prose did not "
                    "share enough tokens or n-grams with the cited raw_quote. Either "
                    "replace the citation with one whose raw_quote actually backs the "
                    "prose, or narrow the prose to match what the cited quote says:\n"
                    f"{weak_json}\n\n"
                )
            revision_block = (
                f"\n=== REVISION MODE (iteration {payload.iteration}) ===\n"
                "Your previous BriefData was reviewed and the Reviewer raised "
                "the issues below. Fix every CRITICAL issue. For each one, "
                "either (a) drop the offending claim, or (b) replace it with "
                "a claim that traces to an evidence_id in the ledger.\n\n"
                "Previous BriefData (for reference, you will produce a new one):\n"
                f"{payload.previous_brief.model_dump_json(indent=2)}\n\n"
                f"Reviewer issues to address:\n{issues_json}\n\n"
                f"{weak_block}"
            )

        user_msg = (
            f"Entity:\n"
            f"  entity_id: {payload.entity.id}\n"
            f"  name: {payload.entity.name}\n"
            f"  type: {payload.entity.type.value}\n"
            f"  primary_url: {payload.entity.primary_url}\n\n"
            f"Verified ledger ({len(ledger_payload)} items):\n"
            f"{json.dumps(ledger_payload, ensure_ascii=False, indent=2)}\n\n"
            f"Estimates available ({len(estimates_payload)}):\n"
            f"{json.dumps(estimates_payload, ensure_ascii=False, indent=2)}\n"
            f"{revision_block}\n"
            "Author the BriefData now. Every text-bearing field must cite "
            "evidence_ids from the ledger above. Use exact UUIDs."
        )

        brief = llm.complete_with_json(
            model=self.model,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            schema=BriefData,
            max_tokens=16384,
            temperature=0.0,
            agent=self.name,
        )

        # ------------------------------------------------------------------
        # Post-validation: every cited evidence_id must exist in the input
        # ledger. Force entity_id to match.
        # ------------------------------------------------------------------
        valid_ids: set[UUID] = {ev.id for ev in payload.ledger}
        if brief.entity_id != payload.entity.id:
            ctx.logger.warning(
                "Author returned entity_id=%s but expected %s — overriding",
                brief.entity_id, payload.entity.id,
            )
            brief = brief.model_copy(update={"entity_id": payload.entity.id})

        cited = brief.all_evidence_ids()
        unknown = cited - valid_ids
        if unknown:
            ctx.logger.warning(
                "Author cited %d evidence_id(s) not in input ledger: %s. "
                "Filtering them out — affected sections may render thinner.",
                len(unknown),
                [str(u) for u in list(unknown)[:5]],
            )
            brief = _filter_unknown_citations(brief, valid_ids)

        return brief


def _filter_unknown_citations(brief: BriefData, valid: set[UUID]) -> BriefData:
    """Strip any evidence_id reference that isn't in `valid`. Items that
    require min_length=1 citations (industries, geographic_footprint, sources)
    get dropped if all citations are invalid."""
    def keep(ids: list[UUID]) -> list[UUID]:
        return [i for i in ids if i in valid]

    industries = [c.model_copy(update={"evidence_ids": keep(c.evidence_ids)})
                  for c in brief.industries
                  if keep(c.evidence_ids)]
    geo = [g.model_copy(update={"evidence_ids": keep(g.evidence_ids)})
           for g in brief.geographic_footprint
           if keep(g.evidence_ids)]
    sources = [s.model_copy(update={"evidence_ids": keep(s.evidence_ids)})
               for s in brief.sources
               if keep(s.evidence_ids)]

    updates: dict[str, Any] = {
        "quick_take": brief.quick_take.model_copy(
            update={"evidence_ids": keep(brief.quick_take.evidence_ids)}),
        "stats": [
            s.model_copy(update={"evidence_id": s.evidence_id if s.evidence_id in valid else None})
            for s in brief.stats
        ],
        "timeline": [t.model_copy(update={"evidence_ids": keep(t.evidence_ids)})
                     for t in brief.timeline],
        "who_they_are_cards": [c.model_copy(update={"evidence_ids": keep(c.evidence_ids)})
                               for c in brief.who_they_are_cards],
        "dna_cards": [c.model_copy(update={"evidence_ids": keep(c.evidence_ids)})
                      for c in brief.dna_cards],
        "industries": industries,
        "geographic_footprint": geo,
        "strategic_signals": [s.model_copy(update={"evidence_ids": keep(s.evidence_ids)})
                              for s in brief.strategic_signals],
        "scorecard": [r.model_copy(update={"evidence_ids": keep(r.evidence_ids)})
                      for r in brief.scorecard],
        "key_signals": [k.model_copy(update={"evidence_ids": keep(k.evidence_ids)})
                        for k in brief.key_signals],
        "recommended_approach": [a.model_copy(update={"evidence_ids": keep(a.evidence_ids)})
                                 for a in brief.recommended_approach],
        "discovery_questions": [q.model_copy(update={"evidence_ids": keep(q.evidence_ids)})
                                for q in brief.discovery_questions],
        "recap_stats": [
            r.model_copy(update={"evidence_id": r.evidence_id if r.evidence_id in valid else None})
            for r in brief.recap_stats
        ],
        "next_steps": [n.model_copy(update={"evidence_ids": keep(n.evidence_ids)})
                       for n in brief.next_steps],
        "sources": sources,
    }
    return brief.model_copy(update=updates)


# Sentinel prose for fields whose citations remain weak after the blocking
# re-pass. Designer renders this as visible "Insufficient public data".
_INSUFFICIENT = "Insufficient public data."


# Map weak-citation location prefixes to the BriefData attribute names whose
# items they index into. Locations come from citation_validator._field_pairs.
_LIST_LOCATION_TO_ATTR = {
    "stats": "stats",
    "timeline": "timeline",
    "who_they_are": "who_they_are_cards",
    "dna": "dna_cards",
    "strategic_signals": "strategic_signals",
    "scorecard": "scorecard",
    "key_signals": "key_signals",
    "recommended_approach": "recommended_approach",
    "discovery_questions": "discovery_questions",
    "next_steps": "next_steps",
}


def _weak_to_review_issues(weak: list[dict]) -> list[ReviewIssue]:
    """Turn semantic-validator flags into ReviewIssue objects so the
    re-author prompt's REVISION MODE block presents them as the Reviewer would.
    """
    return [
        ReviewIssue(
            severity="critical",
            location=w["location"],
            claim=w.get("prose", "")[:200] or "(prose)",
            issue=(
                "Semantic validator: cited evidence_ids share little or no "
                "token / n-gram overlap with this prose. Either rewrite the "
                "prose to match what the cited raw_quote actually says, or "
                "replace the citation with one whose raw_quote backs the claim."
            ),
            suggested_fix=(
                "Pick a different evidence_id from the ledger, or narrow the "
                "prose to a statement the cited quote literally supports."
            ),
        )
        for w in weak
    ]


def _strip_weak_citation_fields(brief: BriefData, weak: list[dict]) -> BriefData:
    """After a failed re-pass, neuter fields that still carry weak citations.

    For singleton text fields (quick_take.body / quick_take.best_angle):
    replace prose with the "Insufficient public data" sentinel and clear
    evidence_ids.

    For indexed list fields (stats[i], who_they_are[i], etc.): drop the entry.
    """
    drop_indexes: dict[str, set[int]] = {}
    replace_singleton: set[str] = set()
    for w in weak:
        loc = w["location"]
        if loc in ("quick_take.body", "quick_take.best_angle"):
            replace_singleton.add(loc)
            continue
        if "[" in loc and loc.endswith("]"):
            base, idx_str = loc[:-1].split("[", 1)
            try:
                idx = int(idx_str)
            except ValueError:
                continue
            drop_indexes.setdefault(base, set()).add(idx)

    updates: dict[str, Any] = {}

    if replace_singleton:
        qt_updates: dict[str, Any] = {}
        if "quick_take.body" in replace_singleton:
            qt_updates["body"] = _INSUFFICIENT
            qt_updates["evidence_ids"] = []
        if "quick_take.best_angle" in replace_singleton:
            qt_updates["best_angle"] = _INSUFFICIENT
        updates["quick_take"] = brief.quick_take.model_copy(update=qt_updates)

    for loc_base, attr in _LIST_LOCATION_TO_ATTR.items():
        bad = drop_indexes.get(loc_base)
        if not bad:
            continue
        original = getattr(brief, attr)
        updates[attr] = [it for i, it in enumerate(original) if i not in bad]

    return brief.model_copy(update=updates) if updates else brief
