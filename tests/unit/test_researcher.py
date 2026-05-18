"""Researcher: low-evidence flag + entity_id consistency, mocking LLMClient."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from account_research.agents.base import PipelineContext
from account_research.agents.researcher import LOW_EVIDENCE_THRESHOLD, ResearcherAgent
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceBatch,
    EvidenceCategory,
    EvidenceItem,
    SourceType,
)


@dataclass
class _FakeLLM:
    """Returns `canned` on the first call; empty batch on subsequent calls
    (so the coverage-loop supplemental round resolves without doubling items)."""
    canned: Any
    calls: int = 0

    def complete_with_json(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return self.canned
        # Subsequent calls (supplemental rounds): return empty batch
        return EvidenceBatch(entity_id=self.canned.entity_id, items=[])


def _ctx(canned) -> PipelineContext:
    return PipelineContext(llm_client=_FakeLLM(canned=canned))


def _full_coverage(entity_id) -> list[EvidenceItem]:
    """Build one item per required category — used by tests that want to
    skip the supplemental coverage round."""
    cats = [
        EvidenceCategory.COMPANY_FACTS,
        EvidenceCategory.FINANCIAL,
        EvidenceCategory.LEADERSHIP,
        EvidenceCategory.PRODUCTS,
        EvidenceCategory.CLIENTS,
        EvidenceCategory.GEOGRAPHY,
    ]
    return [
        EvidenceItem(
            entity_id=entity_id,
            claim=f"Fact for {cat.value}",
            category=cat,
            source_url=f"https://example.com/{cat.value}",
            source_type=SourceType.OFFICIAL_SITE,
            raw_quote=f"Verbatim quote for {cat.value}.",
            fetched_at=datetime.now(timezone.utc),
            confidence=ConfidenceLevel.HIGH,
        )
        for cat in cats
    ]


def _ev(entity_id, n: int) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            entity_id=entity_id,
            claim=f"Fact #{i}",
            category=EvidenceCategory.COMPANY_FACTS,
            source_url=f"https://example.com/{i}",
            source_type=SourceType.OFFICIAL_SITE,
            raw_quote=f"Verbatim quote number {i} from the source page.",
            fetched_at=datetime.now(timezone.utc),
            confidence=ConfidenceLevel.HIGH,
        )
        for i in range(n)
    ]


def test_happy_path_no_flag_when_enough_evidence():
    entity = Entity(name="BWPM", type=EntityType.COMPANY, primary_url="https://bwpm.pro")
    canned = EvidenceBatch(entity_id=entity.id, items=_ev(entity.id, 15))
    out = ResearcherAgent().run(entity, _ctx(canned))
    assert len(out.items) == 15
    assert out.low_evidence_flag is False


def test_low_evidence_flag_set_when_under_threshold():
    entity = Entity(name="ObscureCo", type=EntityType.COMPANY)
    canned = EvidenceBatch(entity_id=entity.id, items=_ev(entity.id, LOW_EVIDENCE_THRESHOLD - 3))
    out = ResearcherAgent().run(entity, _ctx(canned))
    assert out.low_evidence_flag is True
    assert len(out.items) < LOW_EVIDENCE_THRESHOLD


def test_entity_id_mismatch_is_corrected():
    entity = Entity(name="BWPM", type=EntityType.COMPANY)
    wrong_id = uuid4()
    canned = EvidenceBatch(entity_id=wrong_id, items=_ev(wrong_id, 12))
    out = ResearcherAgent().run(entity, _ctx(canned))
    assert out.entity_id == entity.id  # corrected by the agent


def test_supplemental_research_fires_on_empty_category():
    """When a required category has 0 items, the coverage loop runs a second
    LLM call. We inspect the fake's call counter."""
    entity = Entity(name="GapEntity", type=EntityType.COMPANY)
    canned = EvidenceBatch(entity_id=entity.id, items=_ev(entity.id, 12))  # all company_facts
    fake = _FakeLLM(canned=canned)
    ctx = PipelineContext(llm_client=fake)
    ResearcherAgent().run(entity, ctx)
    # First batch is COMPANY_FACTS only; 5 other required categories are empty
    # → supplemental call must fire.
    assert fake.calls == 2


def test_supplemental_round_caps_at_one():
    """Even with persistent gaps, the supplemental round only fires once."""
    entity = Entity(name="GapEntity", type=EntityType.COMPANY)
    canned = EvidenceBatch(entity_id=entity.id, items=_ev(entity.id, 12))
    fake = _FakeLLM(canned=canned)
    ctx = PipelineContext(llm_client=fake)
    ResearcherAgent().run(entity, ctx)
    assert fake.calls == 2, "supplemental round must not recurse"


def test_no_supplemental_when_all_required_categories_covered():
    """Full-coverage ledger → no supplemental call."""
    entity = Entity(name="CompleteCo", type=EntityType.COMPANY)
    canned = EvidenceBatch(entity_id=entity.id, items=_full_coverage(entity.id))
    fake = _FakeLLM(canned=canned)
    ctx = PipelineContext(llm_client=fake)
    out = ResearcherAgent().run(entity, ctx)
    assert fake.calls == 1
    assert len(out.items) == 6  # one per required category


def test_evidence_items_require_raw_quote_at_schema_level():
    """Sanity: the EvidenceItem validator alone enforces the rule;
    the Researcher relies on it, no extra logic needed."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EvidenceItem(
            entity_id=uuid4(),
            claim="Some claim",
            category=EvidenceCategory.COMPANY_FACTS,
            source_url="https://example.com/",
            source_type=SourceType.OFFICIAL_SITE,
            raw_quote="",  # empty — must be rejected
            fetched_at=datetime.now(timezone.utc),
            confidence=ConfidenceLevel.HIGH,
        )
