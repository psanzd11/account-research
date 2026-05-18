"""EstimatorAgent: routes by entity type, collects estimates and insufficient results."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from account_research.agents.base import PipelineContext
from account_research.agents.estimator import (
    APPLICABLE,
    EstimateInput,
    EstimatorAgent,
)
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)


def _vev(claim, raw_quote=None, entity_id=None, source_type=SourceType.OFFICIAL_SITE):
    return VerifiedEvidenceItem(
        entity_id=entity_id or uuid4(),
        claim=claim,
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://example.com/p",
        source_type=source_type,
        raw_quote=raw_quote or claim,
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="verified", method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


def test_company_routes_to_company_recipes():
    assert "consulting_firm_revenue_v1" in APPLICABLE[EntityType.COMPANY]
    assert "saas_revenue_v1" in APPLICABLE[EntityType.COMPANY]


def test_person_routes_to_net_worth():
    assert APPLICABLE[EntityType.PERSON] == ["net_worth_individual_v1"]


def test_company_produces_at_least_one_outcome():
    """A company entity with the BWPM-like ledger produces an Estimate OR
    InsufficientSignals from each applicable recipe — never silently empty."""
    entity = Entity(name="BWPM", type=EntityType.COMPANY,
                    primary_url="https://bwpm.pro")
    ledger = [
        _vev("Founded in 2020 in the Dominican Republic",
             entity_id=entity.id),
        _vev("More than 50 projects delivered",
             raw_quote="más de 50 proyectos", entity_id=entity.id),
        _vev("Active in Dominican Republic, Panamá, Jamaica",
             raw_quote="Servicios en Panamá, Jamaica", entity_id=entity.id),
        _vev("PMI Authorized Training Partner",
             raw_quote="PMI ATP partner", entity_id=entity.id),
        _vev("830 Followers on Instagram",
             raw_quote="830 followers", entity_id=entity.id,
             source_type=SourceType.SOCIAL),
    ]
    result = EstimatorAgent().run(
        EstimateInput(entity=entity, ledger=ledger), PipelineContext()
    )
    total = len(result.estimates) + len(result.insufficient)
    assert total == len(APPLICABLE[EntityType.COMPANY])  # one outcome per applicable recipe

    # At least the consulting recipe should produce a real estimate
    consulting = [e for e in result.estimates if e.method_id == "consulting_firm_revenue_v1"]
    assert len(consulting) == 1
    est = consulting[0]
    assert est.value_range
    assert est.caveat_text


def test_person_with_thin_ledger_returns_insufficient():
    entity = Entity(name="Unknown Person", type=EntityType.PERSON)
    ledger = [_vev("Born somewhere", entity_id=entity.id)]
    result = EstimatorAgent().run(
        EstimateInput(entity=entity, ledger=ledger), PipelineContext()
    )
    assert result.estimates == []
    assert len(result.insufficient) == 1
    assert result.insufficient[0].method_id == "net_worth_individual_v1"


# ---------------------------------------------------------------------------
# Phase 3 — industry-gate tests
# ---------------------------------------------------------------------------


def test_industry_gate_routes_stripe_away_from_saas():
    """Payments-heavy ledger must NOT trigger saas_revenue_v1 estimate."""
    entity = Entity(name="Stripe", type=EntityType.COMPANY,
                    primary_url="https://stripe.com")
    ledger = [
        _vev("Stripe is a payments processing company", entity_id=entity.id),
        _vev("$1.4 trillion in transaction volume processed",
             raw_quote="$1.4T transaction volume", entity_id=entity.id),
        _vev("Card network partnerships with Visa, Mastercard",
             raw_quote="card network integrations", entity_id=entity.id),
        _vev("Merchant acquirer for millions of businesses",
             raw_quote="acquirer for merchants worldwide", entity_id=entity.id),
        _vev("Company has 8000 employees", raw_quote="8000 employees",
             entity_id=entity.id),
        _vev("Founded in 2010", raw_quote="founded 2010", entity_id=entity.id),
    ]
    result = EstimatorAgent().run(
        EstimateInput(entity=entity, ledger=ledger), PipelineContext()
    )
    # No saas_revenue estimate should appear (payments industry has empty recipe list)
    saas_estimates = [e for e in result.estimates if e.method_id == "saas_revenue_v1"]
    assert saas_estimates == [], "saas recipe must not fire on payments ledger"
    # An InsufficientSignals must surface so the abstention is auditable
    abstentions = [
        i for i in result.insufficient
        if i.method_id.startswith("industry_gate(payments)")
    ]
    assert abstentions, "industry-gate should record a payments abstention"


def test_industry_gate_falls_back_when_other():
    """Ambiguous/low-signal ledgers fall back to v1 routing (try all recipes)."""
    entity = Entity(name="BWPM", type=EntityType.COMPANY)
    ledger = [
        _vev("Founded in 2020", entity_id=entity.id),
        _vev("50+ projects delivered",
             raw_quote="50+ projects delivered", entity_id=entity.id),
        _vev("Active in Dominican Republic, Panamá", entity_id=entity.id),
        _vev("PMI Authorized Training Partner", entity_id=entity.id),
    ]
    result = EstimatorAgent().run(
        EstimateInput(entity=entity, ledger=ledger), PipelineContext()
    )
    # Should still produce consulting estimate (existing behavior preserved)
    consulting = [e for e in result.estimates if e.method_id == "consulting_firm_revenue_v1"]
    assert len(consulting) == 1


def test_public_disclosure_short_circuits_heuristic_recipes(monkeypatch):
    """Ledger with an explicit "$5 billion in revenue" quote: public_disclosure_v1
    wins and the heuristic recipes are skipped."""
    entity = Entity(name="AcmeCo", type=EntityType.COMPANY)
    ledger = [
        _vev("Annual revenue of $5 billion reported in 10-K",
             raw_quote="annual revenue of $5 billion",
             entity_id=entity.id),
        _vev("Founded in 2010", entity_id=entity.id),
        _vev("8000 employees", entity_id=entity.id),
    ]
    result = EstimatorAgent().run(
        EstimateInput(entity=entity, ledger=ledger), PipelineContext()
    )
    pd = [e for e in result.estimates if e.method_id == "public_disclosure_v1"]
    saas = [e for e in result.estimates if e.method_id == "saas_revenue_v1"]
    consulting = [e for e in result.estimates if e.method_id == "consulting_firm_revenue_v1"]
    assert len(pd) == 1
    assert pd[0].confidence.value == "high"
    assert saas == [] and consulting == [], "heuristics must be short-circuited"
