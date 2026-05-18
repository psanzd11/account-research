"""Sprint 1.3 — semantic validator becomes blocking, not audit-only.

When `compute_weak_citations` flags citations whose prose doesn't share enough
tokens with the cited `raw_quote`, Author should:

1. Make ONE focused re-author pass with the weak-citation list in the prompt.
2. If, after the re-pass, weak citations remain, replace the offending field's
   prose with "Insufficient public data." (or drop the entry for indexed list
   fields).

We test the count of LLM calls and the post-fallback brief content.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from account_research.agents.author import AuthorAgent, AuthorInput
from account_research.agents.base import PipelineContext
from account_research.schemas.brief import (
    BriefData,
    HeroSection,
    QuickTake,
)
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)


@dataclass
class _SequencedFakeLLM:
    """Returns canned responses in order; raises if asked for more than provided."""

    responses: list[Any]
    calls: list[dict] = field(default_factory=list)

    def complete_with_json(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) > len(self.responses):
            raise AssertionError(
                f"LLM called {len(self.calls)} times but only "
                f"{len(self.responses)} responses configured."
            )
        return self.responses[len(self.calls) - 1]


def _ev(entity_id, raw_quote: str) -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=entity_id,
        claim="A claim",
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://x.test/p",
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote=raw_quote,
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="verified", method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


def _weak_brief(entity_id, ev_id):
    """Brief whose quick_take.body shares no tokens with the cited evidence."""
    return BriefData(
        entity_id=entity_id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(
            body="Operates aerospace satellite manufacturing in 12 continents.",
            evidence_ids=[ev_id],
        ),
    )


def _clean_brief(entity_id, ev_id):
    """Brief whose quick_take.body paraphrases the cited evidence cleanly."""
    return BriefData(
        entity_id=entity_id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(
            body="The company has 250 employees across four offices worldwide.",
            evidence_ids=[ev_id],
        ),
    )


def test_no_repass_when_no_weak_citations():
    entity = Entity(name="X", type=EntityType.COMPANY)
    ev = _ev(entity.id, "The company has 250 employees across four offices")
    clean = _clean_brief(entity.id, ev.id)
    fake = _SequencedFakeLLM(responses=[clean])
    out = AuthorAgent().run(
        AuthorInput(entity=entity, ledger=[ev], estimates=[]),
        PipelineContext(llm_client=fake),
    )
    assert len(fake.calls) == 1
    assert out.quick_take.body == clean.quick_take.body


def test_one_repass_when_weak_citation_then_clean():
    """First emission is weak → Author re-authors → second emission is clean."""
    entity = Entity(name="X", type=EntityType.COMPANY)
    ev = _ev(entity.id, "The company has 250 employees across four offices")
    weak = _weak_brief(entity.id, ev.id)
    clean = _clean_brief(entity.id, ev.id)
    fake = _SequencedFakeLLM(responses=[weak, clean])
    out = AuthorAgent().run(
        AuthorInput(entity=entity, ledger=[ev], estimates=[]),
        PipelineContext(llm_client=fake),
    )
    assert len(fake.calls) == 2, (
        "Author should re-author once when weak citations are detected."
    )
    # Final brief is the clean one
    assert out.quick_take.body == clean.quick_take.body
    # Re-pass prompt should mention semantic validator
    second_prompt = fake.calls[1]["messages"][0]["content"]
    assert (
        "semantic" in second_prompt.lower()
        or "weak citation" in second_prompt.lower()
    ), "Re-pass prompt should reference the weak-citation feedback"


def test_repass_then_fallback_when_still_weak():
    """Both emissions are weak → final brief replaces quick_take.body with the
    'Insufficient public data' fallback."""
    entity = Entity(name="X", type=EntityType.COMPANY)
    ev = _ev(entity.id, "The company has 250 employees across four offices")
    weak = _weak_brief(entity.id, ev.id)
    weak_again = _weak_brief(entity.id, ev.id)
    fake = _SequencedFakeLLM(responses=[weak, weak_again])
    out = AuthorAgent().run(
        AuthorInput(entity=entity, ledger=[ev], estimates=[]),
        PipelineContext(llm_client=fake),
    )
    assert len(fake.calls) == 2
    assert "Insufficient public data" in out.quick_take.body
    # The evidence_ids for that field should be cleared since the prose
    # no longer makes a sourced claim.
    assert out.quick_take.evidence_ids == []
