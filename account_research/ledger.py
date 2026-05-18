"""Evidence ledger: ORM models + module-level API.

Matches SPEC §5.1. Schemas in account_research.schemas are the Pydantic
in-memory representations; the ORM rows here are the on-disk form. Conversion
helpers (`to_pydantic` / `from_pydantic`) bridge the two so agents stay on
Pydantic and the DB layer stays on SQLAlchemy.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from account_research.db import Base
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.estimate import Estimate, SignalUsed
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceItem,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------


class EntityRow(Base):
    __tablename__ = "entities"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    primary_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    evidence: Mapped[list["EvidenceRow"]] = relationship(back_populates="entity", cascade="all, delete-orphan")
    estimates: Mapped[list["EstimateRow"]] = relationship(back_populates="entity", cascade="all, delete-orphan")


class EvidenceRow(Base):
    __tablename__ = "evidence_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("entities.id"), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_quote: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    verification_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verification_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verification_checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    verification_similarity: Mapped[float | None] = mapped_column(nullable=True)

    entity: Mapped[EntityRow] = relationship(back_populates="evidence")


class EstimateRow(Base):
    __tablename__ = "estimates"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("entities.id"), nullable=False)
    metric: Mapped[str] = mapped_column(String(128), nullable=False)
    applies_to: Mapped[str] = mapped_column(String(32), nullable=False)
    value_range: Mapped[str] = mapped_column(String(128), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    method_id: Mapped[str] = mapped_column(String(128), nullable=False)
    signals_used: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    assumptions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    caveat_text: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    entity: Mapped[EntityRow] = relationship(back_populates="estimates")


class PipelineRunRow(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    entity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("entities.id"), nullable=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    iterations: Mapped[int] = mapped_column(default=0, nullable=False)
    final_pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    issues: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)


# ---------------------------------------------------------------------------
# Pydantic <-> ORM conversions
# ---------------------------------------------------------------------------


def _entity_to_row(e: Entity) -> EntityRow:
    return EntityRow(
        id=e.id,
        name=e.name,
        type=e.type.value,
        primary_url=str(e.primary_url) if e.primary_url else None,
        aliases=list(e.aliases),
        created_at=e.created_at,
    )


def _row_to_entity(r: EntityRow) -> Entity:
    return Entity(
        id=r.id,
        name=r.name,
        type=EntityType(r.type),
        primary_url=r.primary_url,
        aliases=list(r.aliases or []),
        created_at=r.created_at,
    )


def _evidence_to_row(ev: EvidenceItem | VerifiedEvidenceItem) -> EvidenceRow:
    row = EvidenceRow(
        id=ev.id,
        entity_id=ev.entity_id,
        claim=ev.claim,
        category=ev.category.value,
        source_url=str(ev.source_url),
        source_type=ev.source_type.value,
        raw_quote=ev.raw_quote,
        fetched_at=ev.fetched_at,
        confidence=ev.confidence.value,
        notes=ev.notes,
    )
    if isinstance(ev, VerifiedEvidenceItem):
        row.verification_status = ev.verification.status
        row.verification_method = ev.verification.method
        row.verification_checked_at = ev.verification.checked_at
        row.verification_similarity = ev.verification.similarity
    return row


def _row_to_evidence(r: EvidenceRow) -> EvidenceItem | VerifiedEvidenceItem:
    base = dict(
        id=r.id,
        entity_id=r.entity_id,
        claim=r.claim,
        category=EvidenceCategory(r.category),
        source_url=r.source_url,
        source_type=SourceType(r.source_type),
        raw_quote=r.raw_quote,
        fetched_at=r.fetched_at,
        confidence=ConfidenceLevel(r.confidence),
        notes=r.notes,
    )
    if r.verification_status:
        return VerifiedEvidenceItem(
            **base,
            verification=Verification(
                status=r.verification_status,  # type: ignore[arg-type]
                method=r.verification_method,  # type: ignore[arg-type]
                checked_at=r.verification_checked_at,
                similarity=r.verification_similarity,
            ),
        )
    return EvidenceItem(**base)


def _estimate_to_row(est: Estimate) -> EstimateRow:
    return EstimateRow(
        id=est.id,
        entity_id=est.entity_id,
        metric=est.metric,
        applies_to=est.applies_to,
        value_range=est.value_range,
        unit=est.unit,
        confidence=est.confidence.value,
        method_id=est.method_id,
        signals_used=[s.model_dump(mode="json") for s in est.signals_used],
        assumptions=list(est.assumptions),
        caveat_text=est.caveat_text,
        computed_at=est.computed_at,
    )


def _row_to_estimate(r: EstimateRow) -> Estimate:
    return Estimate(
        id=r.id,
        entity_id=r.entity_id,
        metric=r.metric,
        applies_to=r.applies_to,  # type: ignore[arg-type]
        value_range=r.value_range,
        unit=r.unit,
        confidence=ConfidenceLevel(r.confidence),
        method_id=r.method_id,
        signals_used=[SignalUsed.model_validate(s) for s in (r.signals_used or [])],
        assumptions=list(r.assumptions or []),
        caveat_text=r.caveat_text,
        computed_at=r.computed_at,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def insert_entity(session: Session, entity: Entity) -> Entity:
    row = _entity_to_row(entity)
    session.add(row)
    session.flush()
    return _row_to_entity(row)


def get_entity(session: Session, entity_id: UUID) -> Entity | None:
    row = session.get(EntityRow, entity_id)
    return _row_to_entity(row) if row else None


def insert_evidence(session: Session, evidence: EvidenceItem | VerifiedEvidenceItem) -> None:
    session.add(_evidence_to_row(evidence))
    session.flush()


def insert_evidence_bulk(
    session: Session, items: Iterable[EvidenceItem | VerifiedEvidenceItem]
) -> int:
    rows = [_evidence_to_row(ev) for ev in items]
    session.add_all(rows)
    session.flush()
    return len(rows)


def get_evidence_for_entity(
    session: Session, entity_id: UUID
) -> list[EvidenceItem | VerifiedEvidenceItem]:
    rows = (
        session.query(EvidenceRow)
        .filter(EvidenceRow.entity_id == entity_id)
        .order_by(EvidenceRow.fetched_at.asc())
        .all()
    )
    return [_row_to_evidence(r) for r in rows]


def mark_verified(session: Session, evidence_id: UUID, verification: Verification) -> None:
    row = session.get(EvidenceRow, evidence_id)
    if row is None:
        raise KeyError(f"evidence_item {evidence_id} not found")
    row.verification_status = verification.status
    row.verification_method = verification.method
    row.verification_checked_at = verification.checked_at
    row.verification_similarity = verification.similarity
    session.flush()


def insert_estimate(session: Session, estimate: Estimate) -> None:
    session.add(_estimate_to_row(estimate))
    session.flush()


def get_estimates_for_entity(session: Session, entity_id: UUID) -> list[Estimate]:
    rows = (
        session.query(EstimateRow)
        .filter(EstimateRow.entity_id == entity_id)
        .order_by(EstimateRow.computed_at.asc())
        .all()
    )
    return [_row_to_estimate(r) for r in rows]


def start_run(session: Session, query: str, entity_id: UUID | None = None) -> UUID:
    row = PipelineRunRow(query=query, entity_id=entity_id, status="running")
    session.add(row)
    session.flush()
    return row.id


def end_run(
    session: Session,
    run_id: UUID,
    *,
    status: str,
    iterations: int = 0,
    final_pdf_path: str | None = None,
    issues: list[dict] | None = None,
) -> None:
    row = session.get(PipelineRunRow, run_id)
    if row is None:
        raise KeyError(f"pipeline_run {run_id} not found")
    row.status = status
    row.completed_at = datetime.now(timezone.utc)
    row.iterations = iterations
    row.final_pdf_path = final_pdf_path
    row.issues = issues or []
    session.flush()
