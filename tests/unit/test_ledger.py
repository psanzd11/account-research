"""Ledger CRUD + Pydantic↔ORM roundtrip."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from account_research.ledger import (
    get_estimates_for_entity,
    get_evidence_for_entity,
    insert_estimate,
    insert_evidence,
    mark_verified,
    start_run,
    end_run,
)
from account_research.schemas import (
    ConfidenceLevel,
    Entity,
    Estimate,
    EvidenceCategory,
    EvidenceItem,
    SourceType,
    Verification,
)


def _make_ev(entity: Entity, quote: str = "Quote text here") -> EvidenceItem:
    return EvidenceItem(
        entity_id=entity.id,
        claim="A claim.",
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://example.com/path",
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote=quote,
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
    )


def test_evidence_roundtrip(session: Session, sample_entity: Entity):
    ev = _make_ev(sample_entity)
    insert_evidence(session, ev)

    items = get_evidence_for_entity(session, sample_entity.id)
    assert len(items) == 1
    assert items[0].raw_quote == "Quote text here"
    assert items[0].entity_id == sample_entity.id


def test_mark_verified_persists(session: Session, sample_entity: Entity):
    ev = _make_ev(sample_entity)
    insert_evidence(session, ev)

    mark_verified(
        session,
        ev.id,
        Verification(
            status="verified",
            method="exact_match",
            checked_at=datetime.now(timezone.utc),
        ),
    )

    items = get_evidence_for_entity(session, sample_entity.id)
    assert hasattr(items[0], "verification")
    assert items[0].verification.status == "verified"  # type: ignore[union-attr]


def test_estimate_roundtrip(session: Session, sample_entity: Entity):
    est = Estimate(
        entity_id=sample_entity.id,
        metric="estimated_annual_revenue",
        applies_to="company",
        value_range="$0.5-2M",
        unit="USD/year",
        confidence=ConfidenceLevel.LOW,
        method_id="consulting_firm_revenue_v1",
        caveat_text="Estimated.",
    )
    insert_estimate(session, est)

    results = get_estimates_for_entity(session, sample_entity.id)
    assert len(results) == 1
    assert results[0].value_range == "$0.5-2M"
    assert results[0].method_id == "consulting_firm_revenue_v1"


def test_pipeline_run_lifecycle(session: Session, sample_entity: Entity):
    run_id = start_run(session, query="BW Project Management", entity_id=sample_entity.id)
    end_run(session, run_id, status="completed", iterations=2, final_pdf_path="/tmp/x.pdf")
    # No assertion on shape beyond "no exception"; the row exists.
