"""Estimator — produce ranged financial estimates from the verified ledger.

SPEC §4.3. Pure computation, no LLM (recipes do regex/heuristic signal
extraction). Routes by entity_type AND industry (when classifiable) to the
recipes worth trying; each recipe self-gates on minimum_required_signals.

Feature flag ESTIMATOR_INDUSTRY_GATE (default ON):
  - Always tries `public_disclosure_v1` first for companies — if it returns
    an Estimate, we short-circuit and skip heuristic recipes entirely.
  - Otherwise routes by industry → recipes via APPLICABLE_INDUSTRY map.
  - Falls back to entity-type routing (APPLICABLE) when industry is "other".
"""
from __future__ import annotations

import os
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from account_research.agents.base import BaseAgent, PipelineContext
from account_research.agents.industry_classifier import (
    Industry,
    classify_industry,
    industry_scores,
)
from account_research.methodology import recipes as _recipes  # noqa: F401 — registers
from account_research.methodology.loader import dispatch, is_registered
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.estimate import Estimate, InsufficientSignals
from account_research.schemas.evidence import VerifiedEvidenceItem

_INDUSTRY_GATE_ENABLED = (
    os.environ.get("ESTIMATOR_INDUSTRY_GATE", "1").lower()
    not in ("0", "false", "no", "off")
)


# Mapping from entity type to recipes worth trying. Each recipe self-gates
# via minimum_required_signals, so it's safe to attempt multiple. Used as the
# fallback when industry classification is "other".
APPLICABLE: dict[EntityType, list[str]] = {
    EntityType.COMPANY: ["consulting_firm_revenue_v1", "saas_revenue_v1"],
    EntityType.PERSON: ["net_worth_individual_v1"],
}

# Industry → applicable recipes for companies. An empty list means "no
# heuristic recipe is a credible fit; emit InsufficientSignals via the
# loader rather than misfire."
APPLICABLE_INDUSTRY: dict[Industry, list[str]] = {
    "saas": ["saas_revenue_v1"],
    "consulting": ["consulting_firm_revenue_v1"],
    "payments": [],     # Stripe-class: no heuristic recipe fits
    "fintech": [],      # diverse models; await dedicated recipe
    "ecommerce": [],
    "marketplace": [],
    "ai": [],
    "hardware": [],
    "media": [],
    "other": [],        # falls back to APPLICABLE[type] below
}


class EstimateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    entity: Entity
    ledger: list[VerifiedEvidenceItem]
    # G2: pre-computed corroboration map ({evidence_id: group_size}). When
    # supplied, recipes count multi-source-corroborated signals 1.5×.
    corroboration_map: dict = Field(default_factory=dict)


class EstimateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimates: list[Estimate] = Field(default_factory=list)
    insufficient: list[InsufficientSignals] = Field(default_factory=list)


def _company_recipes_for(ledger: list[VerifiedEvidenceItem]) -> list[str]:
    """Resolve the recipe list for a company entity under the industry gate.

    - Always try public_disclosure_v1 first (handled by caller).
    - Then route by classified industry.
    - If industry is "other" (or below confidence), fall back to APPLICABLE
      (both consulting + saas) so we keep v1 behavior for genuine ambiguity.
    """
    industry = classify_industry(ledger)
    industry_recipes = APPLICABLE_INDUSTRY.get(industry, [])
    if industry == "other":
        return APPLICABLE[EntityType.COMPANY]
    return industry_recipes


class EstimatorAgent(BaseAgent[EstimateInput, EstimateResult]):
    name = "estimator"
    model = ""  # No LLM
    input_schema = EstimateInput
    output_schema = EstimateResult

    def run(self, payload: EstimateInput, ctx: PipelineContext) -> EstimateResult:
        estimates: list[Estimate] = []
        insufficient: list[InsufficientSignals] = []

        # B3: companies — try public_disclosure_v1 first. If it returns an
        # Estimate, short-circuit; the heuristic recipes are skipped.
        public_short_circuited = False
        if payload.entity.type == EntityType.COMPANY and _INDUSTRY_GATE_ENABLED:
            if is_registered("public_disclosure_v1"):
                result = dispatch(
                    "public_disclosure_v1",
                    entity_id=payload.entity.id,
                    ledger=payload.ledger,
                    corroboration_map=payload.corroboration_map or None,
                )
                if isinstance(result, Estimate):
                    estimates.append(result)
                    public_short_circuited = True
                    ctx.logger.info(
                        "Estimator: public_disclosure_v1 short-circuited heuristics "
                        "(value=%s, confidence=%s)",
                        result.value_range, result.confidence.value,
                    )

        # Build the recipe list for the heuristic pass.
        if public_short_circuited:
            method_ids: list[str] = []
        elif payload.entity.type == EntityType.COMPANY and _INDUSTRY_GATE_ENABLED:
            industry = classify_industry(payload.ledger)
            scores = industry_scores(payload.ledger)
            ctx.logger.info(
                "Estimator: industry=%s (scores=%s)",
                industry, dict(scores.most_common(4)),
            )
            method_ids = _company_recipes_for(payload.ledger)
            if not method_ids:
                # Industry classified but no recipe fits → emit a single
                # InsufficientSignals so downstream code knows we abstained
                # on purpose.
                insufficient.append(
                    InsufficientSignals(
                        metric="estimated_annual_revenue",
                        method_id=f"industry_gate({industry})",
                        signals_present=0,
                        signals_required=1,
                        missing=[f"recipe_for_industry={industry}"],
                    )
                )
                ctx.logger.info(
                    "Estimator: no recipe for industry=%s — abstaining", industry,
                )
        else:
            method_ids = APPLICABLE.get(payload.entity.type, [])

        for method_id in method_ids:
            if not is_registered(method_id):
                ctx.logger.warning(
                    "Recipe %s not registered — skipping. Ensure "
                    "account_research.methodology.recipes is importable.",
                    method_id,
                )
                continue
            result = dispatch(
                method_id,
                entity_id=payload.entity.id,
                ledger=payload.ledger,
                corroboration_map=payload.corroboration_map or None,
            )
            if isinstance(result, Estimate):
                estimates.append(result)
                ctx.logger.info(
                    "Estimator: %s -> %s (confidence=%s)",
                    method_id, result.value_range, result.confidence.value,
                )
            else:
                insufficient.append(result)
                ctx.logger.info(
                    "Estimator: %s -> insufficient signals (%d/%d, missing=%s)",
                    method_id,
                    result.signals_present,
                    result.signals_required,
                    result.missing,
                )

        return EstimateResult(estimates=estimates, insufficient=insufficient)


def estimate_for_entity(
    entity: Entity,
    ledger: Iterable[VerifiedEvidenceItem],
    ctx: PipelineContext,
) -> EstimateResult:
    return EstimatorAgent().run(
        EstimateInput(entity=entity, ledger=list(ledger)), ctx
    )
