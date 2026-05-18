"""Disambiguator: ok / ambiguous / not_found, mocking LLMClient.complete_with_json."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from account_research.agents.base import PipelineContext
from account_research.agents.disambiguator import DisambiguatorAgent
from account_research.schemas.entity import (
    CandidateOption,
    DisambigInput,
    DisambigResult,
    Entity,
    EntityType,
)


@dataclass
class _FakeLLM:
    """Returns a pre-canned response from complete_with_json."""

    canned: Any

    def complete_with_json(self, **kwargs):
        return self.canned


def _ctx(canned) -> PipelineContext:
    return PipelineContext(llm_client=_FakeLLM(canned=canned))


def test_ok_path_returns_entity():
    entity = Entity(name="BW Project Management", type=EntityType.COMPANY, primary_url="https://bwpm.pro")
    canned = DisambigResult(status="ok", entity=entity)
    out = DisambiguatorAgent().run(
        DisambigInput(query="BWPM", entity_type_hint="company"),
        _ctx(canned),
    )
    assert out.status == "ok"
    assert out.entity is not None
    assert out.entity.name == "BW Project Management"


def test_ambiguous_path_preserves_candidates():
    cand = [
        CandidateOption(name="BW Project Management", type=EntityType.COMPANY,
                        primary_url="https://bwpm.pro", confidence_score=0.78, rationale="DR PM consultancy"),
        CandidateOption(name="B&W Group", type=EntityType.COMPANY,
                        primary_url="https://bw-group.example", confidence_score=0.62, rationale="Maritime services"),
    ]
    canned = DisambigResult(status="ambiguous", candidates_top3=cand)
    out = DisambiguatorAgent().run(
        DisambigInput(query="BW"),
        _ctx(canned),
    )
    assert out.status == "ambiguous"
    assert len(out.candidates_top3) == 2
    assert out.entity is None


def test_not_found_returns_no_entity():
    canned = DisambigResult(status="not_found", notes="No public footprint")
    out = DisambiguatorAgent().run(
        DisambigInput(query="ZxQ Phantom Co"),
        _ctx(canned),
    )
    assert out.status == "not_found"
    assert out.entity is None


def test_requires_llm_in_context():
    ctx = PipelineContext()  # no llm_client
    with pytest.raises(RuntimeError, match="llm_client"):
        DisambiguatorAgent().run(DisambigInput(query="x"), ctx)
