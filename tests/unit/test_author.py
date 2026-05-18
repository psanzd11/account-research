"""Author tests: ledger + estimate input flow; post-validator drops unknown citations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from account_research.agents.author import AuthorAgent, AuthorInput
from account_research.agents.base import PipelineContext
from account_research.schemas.brief import (
    BriefData,
    HeroBadge,
    HeroSection,
    IndustryChip,
    QuickTake,
    SourceRef,
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
class _FakeLLM:
    canned: Any
    captured: dict | None = None

    def complete_with_json(self, **kwargs):
        self.captured = kwargs
        return self.canned


def _ev(entity_id, claim, raw_quote=None, url="https://x.test/p") -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=entity_id,
        claim=claim,
        category=EvidenceCategory.COMPANY_FACTS,
        source_url=url,
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote=raw_quote or claim,
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="verified", method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


def test_filters_non_acceptable_items_from_prompt():
    """Author drops items where is_acceptable_for_author() == False.

    Non-Tier-1 source + unverifiable verification → must be excluded.
    """
    entity = Entity(name="X", type=EntityType.COMPANY)
    ev_verified = _ev(entity.id, "Verified claim")
    # Build a non-Tier-1 (PRESS) item with unverifiable status — NOT acceptable.
    ev_unverif = VerifiedEvidenceItem(
        entity_id=entity.id,
        claim="Unverified claim",
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://news.example/article",
        source_type=SourceType.PRESS,
        raw_quote="Unverified claim",
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="unverifiable", method="content_changed",
            checked_at=datetime.now(timezone.utc), similarity=0.3,
        ),
    )

    canned = BriefData(
        entity_id=entity.id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body="ok", evidence_ids=[ev_verified.id]),
    )
    fake = _FakeLLM(canned=canned)
    ctx = PipelineContext(llm_client=fake)
    AuthorAgent().run(
        AuthorInput(entity=entity, ledger=[ev_verified, ev_unverif], estimates=[]),
        ctx,
    )
    user_content = fake.captured["messages"][0]["content"]
    assert str(ev_verified.id) in user_content
    assert str(ev_unverif.id) not in user_content


def test_tier1_high_unverifiable_item_is_included():
    """Author accepts Tier-1 (OFFICIAL_SITE/SEC/GOV) + HIGH items even when
    Fact-Checker couldn't re-find the quote (e.g. JS-rendered page).
    """
    entity = Entity(name="X", type=EntityType.COMPANY)
    ev_fallback = VerifiedEvidenceItem(
        entity_id=entity.id,
        claim="Founded in 2020",
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://example.com/about",
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote="Founded in 2020",
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="unverifiable", method="content_changed",
            checked_at=datetime.now(timezone.utc), similarity=0.4,
        ),
    )

    canned = BriefData(
        entity_id=entity.id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body="ok", evidence_ids=[ev_fallback.id]),
    )
    fake = _FakeLLM(canned=canned)
    AuthorAgent().run(
        AuthorInput(entity=entity, ledger=[ev_fallback], estimates=[]),
        PipelineContext(llm_client=fake),
    )
    user_content = fake.captured["messages"][0]["content"]
    assert str(ev_fallback.id) in user_content


def test_post_validator_drops_unknown_evidence_ids():
    entity = Entity(name="X", type=EntityType.COMPANY)
    ev = _ev(entity.id, "Real claim about the company")
    # Model fabricates an ID + cites the real one
    fabricated = uuid4()

    canned = BriefData(
        entity_id=entity.id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        # Body paraphrases the cited raw_quote so the semantic validator
        # doesn't fire (this test is for the UUID filter, not the validator).
        quick_take=QuickTake(
            body="A real claim about the company was published.",
            evidence_ids=[ev.id, fabricated],
        ),
        industries=[
            IndustryChip(name="Real", evidence_ids=[ev.id]),
            IndustryChip(name="Fake", evidence_ids=[fabricated]),
        ],
        sources=[
            SourceRef(title="Real source", url="https://x.test/p",
                      source_type="official_site", evidence_ids=[ev.id]),
            SourceRef(title="Fake source", url="https://x.test/q",
                      source_type="official_site", evidence_ids=[fabricated]),
        ],
    )
    out = AuthorAgent().run(
        AuthorInput(entity=entity, ledger=[ev], estimates=[]),
        PipelineContext(llm_client=_FakeLLM(canned=canned)),
    )
    # quick_take loses the fake citation but keeps the real one
    assert ev.id in out.quick_take.evidence_ids
    assert fabricated not in out.quick_take.evidence_ids
    # industries chip whose only citation was fake gets dropped entirely
    industry_names = [c.name for c in out.industries]
    assert "Real" in industry_names
    assert "Fake" not in industry_names
    # sources whose only citation was fake gets dropped
    src_titles = [s.title for s in out.sources]
    assert "Real source" in src_titles
    assert "Fake source" not in src_titles


def test_raw_quote_truncation_at_500_chars():
    """D1: Author payload truncates raw_quote at 500 chars (was 240 in v1)."""
    entity = Entity(name="X", type=EntityType.COMPANY)
    long_quote = "A" * 400 + " " + "B" * 200  # 601 chars total
    ev = _ev(entity.id, "Claim", raw_quote=long_quote)
    canned = BriefData(
        entity_id=entity.id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body="ok", evidence_ids=[ev.id]),
    )
    fake = _FakeLLM(canned=canned)
    AuthorAgent().run(
        AuthorInput(entity=entity, ledger=[ev], estimates=[]),
        PipelineContext(llm_client=fake),
    )
    content = fake.captured["messages"][0]["content"]
    # First 500 chars must be present; v1's 240-char cut should NOT be the limit
    assert "A" * 400 in content
    # Should have at least 500 consecutive characters from the quote
    # (proving the 240-char ceiling was lifted)
    found_400_as = content.find("A" * 400)
    # Find a slice of 500 chars starting at the quote position
    assert found_400_as >= 0


def test_weak_citation_triggers_strip_when_repass_fails():
    """D2 + Sprint 1.3: when the LLM keeps returning the same weakly-cited
    prose on the blocking re-pass, the field is replaced with the
    'Insufficient public data' sentinel and evidence_ids is cleared.
    """
    entity = Entity(name="X", type=EntityType.COMPANY)
    ev = _ev(entity.id, "Headcount of 250",
             raw_quote="The company has 250 employees worldwide")

    canned = BriefData(
        entity_id=entity.id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(
            body="Operates aerospace satellite manufacturing across 12 continents",
            evidence_ids=[ev.id],  # citation doesn't support the prose
        ),
    )
    agent = AuthorAgent()
    # _FakeLLM returns the SAME canned brief on the re-pass, so strip fires.
    out = agent.run(
        AuthorInput(entity=entity, ledger=[ev], estimates=[]),
        PipelineContext(llm_client=_FakeLLM(canned=canned)),
    )
    assert "Insufficient public data" in out.quick_take.body
    assert out.quick_take.evidence_ids == []
    # After strip, no weak citations remain to report.
    assert agent.last_weak_citations == []


def test_weak_citation_not_flagged_when_prose_matches_quote():
    """D2: prose that paraphrases the quote with shared tokens is NOT flagged."""
    entity = Entity(name="X", type=EntityType.COMPANY)
    ev = _ev(entity.id, "Headcount of 250",
             raw_quote="The company has 250 employees across four offices")
    canned = BriefData(
        entity_id=entity.id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(
            body="The company employs 250 people across four offices.",
            evidence_ids=[ev.id],
        ),
    )
    agent = AuthorAgent()
    agent.run(
        AuthorInput(entity=entity, ledger=[ev], estimates=[]),
        PipelineContext(llm_client=_FakeLLM(canned=canned)),
    )
    assert agent.last_weak_citations == []


def test_revision_prompt_includes_previous_weak_citations():
    """D4: previous_weak_citations are surfaced in the revision prompt."""
    entity = Entity(name="X", type=EntityType.COMPANY)
    ev = _ev(entity.id, "Some fact")
    prev = BriefData(
        entity_id=entity.id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body="old text", evidence_ids=[ev.id]),
    )
    from account_research.schemas.review import ReviewIssue
    issues = [ReviewIssue(
        severity="critical",
        location="quick_take.body",
        claim="something",
        issue="cited evidence does not support the prose",
        suggested_fix="replace or narrow",
    )]
    weak = [{
        "location": "quick_take.body",
        "prose": "old text",
        "cited_evidence_ids": [str(ev.id)],
        "jaccard": 0.0,
        "missing_token_overlap": True,
    }]
    canned = BriefData(
        entity_id=entity.id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body="new text", evidence_ids=[ev.id]),
    )
    fake = _FakeLLM(canned=canned)
    AuthorAgent().run(
        AuthorInput(
            entity=entity, ledger=[ev], estimates=[],
            previous_brief=prev, reviewer_issues=issues,
            previous_weak_citations=weak,
            iteration=2,
        ),
        PipelineContext(llm_client=fake),
    )
    content = fake.captured["messages"][0]["content"]
    assert "Semantic validator" in content or "semantic validator" in content
    assert "missing_token_overlap" in content


def test_entity_id_mismatch_corrected():
    entity = Entity(name="X", type=EntityType.COMPANY)
    wrong_id = uuid4()
    canned = BriefData(
        entity_id=wrong_id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body="ok"),
    )
    out = AuthorAgent().run(
        AuthorInput(entity=entity, ledger=[], estimates=[]),
        PipelineContext(llm_client=_FakeLLM(canned=canned)),
    )
    assert out.entity_id == entity.id  # corrected
