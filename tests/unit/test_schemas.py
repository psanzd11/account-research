"""Schema validation tests — happy paths + the rejections CLAUDE.md mandates."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from account_research.schemas import (
    BriefData,
    ConfidenceLevel,
    Entity,
    EntityType,
    Estimate,
    EvidenceCategory,
    EvidenceItem,
    HeroSection,
    QuickTake,
    SourceType,
)


def _ev_kwargs(entity_id):
    return dict(
        entity_id=entity_id,
        claim="Founded in 2020 in the Dominican Republic",
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://bwpm.pro/about/",
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote="Desde nuestros inicios en 2020 en la República Dominicana",
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
    )


class TestEvidenceItem:
    def test_happy_path(self):
        ev = EvidenceItem(**_ev_kwargs(uuid4()))
        assert ev.raw_quote.startswith("Desde")

    def test_rejects_empty_raw_quote(self):
        kw = _ev_kwargs(uuid4()) | {"raw_quote": ""}
        with pytest.raises(ValidationError):
            EvidenceItem(**kw)

    def test_rejects_whitespace_only_quote(self):
        kw = _ev_kwargs(uuid4()) | {"raw_quote": "   "}
        with pytest.raises(ValidationError):
            EvidenceItem(**kw)

    def test_rejects_placeholder_quote(self):
        for placeholder in ("N/A", "tbd", "unknown", "..."):
            kw = _ev_kwargs(uuid4()) | {"raw_quote": placeholder}
            with pytest.raises(ValidationError):
                EvidenceItem(**kw)


class TestEstimate:
    def _kw(self):
        return dict(
            entity_id=uuid4(),
            metric="estimated_annual_revenue",
            applies_to="company",
            value_range="$0.5-2M",
            unit="USD/year",
            confidence=ConfidenceLevel.LOW,
            method_id="consulting_firm_revenue_v1",
            caveat_text="Estimated from indirect signals.",
        )

    def test_happy_path_with_dash(self):
        est = Estimate(**self._kw())
        assert est.value_range == "$0.5-2M"

    def test_accepts_en_dash(self):
        est = Estimate(**self._kw() | {"value_range": "$15–30M"})
        assert "–" in est.value_range

    def test_accepts_word_to(self):
        est = Estimate(**self._kw() | {"value_range": "1 to 5M"})
        assert "to" in est.value_range

    def test_rejects_point_value(self):
        with pytest.raises(ValidationError):
            Estimate(**self._kw() | {"value_range": "$5M"})

    def test_rejects_unknown_confidence(self):
        with pytest.raises(ValidationError):
            Estimate(**self._kw() | {"confidence": ConfidenceLevel.UNKNOWN})


class TestBriefData:
    def test_minimal_brief_validates(self):
        entity_id = uuid4()
        brief = BriefData(
            entity_id=entity_id,
            hero=HeroSection(name="Test Co", entity_type=EntityType.COMPANY),
            quick_take=QuickTake(body="A short take.", evidence_ids=[uuid4()]),
        )
        assert brief.entity_id == entity_id
        assert brief.industries == []  # empty by default — Designer omits the section

    def test_all_evidence_ids_aggregates_correctly(self):
        ev1 = uuid4()
        ev2 = uuid4()
        brief = BriefData(
            entity_id=uuid4(),
            hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
            quick_take=QuickTake(body="x", evidence_ids=[ev1]),
        )
        from account_research.schemas import IndustryChip

        brief.industries.append(IndustryChip(name="Technology", evidence_ids=[ev2]))
        ids = brief.all_evidence_ids()
        assert ids == {ev1, ev2}


class TestEntity:
    def test_company_entity(self):
        e = Entity(name="Stripe", type=EntityType.COMPANY)
        assert e.type == EntityType.COMPANY
        assert e.aliases == []

    def test_rejects_empty_name(self):
        with pytest.raises(ValidationError):
            Entity(name="", type=EntityType.COMPANY)
