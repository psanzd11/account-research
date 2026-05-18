"""Author revision-mode: previous_brief + reviewer_issues steer the redraft."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from account_research.agents.author import AuthorAgent, AuthorInput
from account_research.agents.base import PipelineContext
from account_research.schemas.brief import BriefData, HeroSection, QuickTake
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.review import ReviewIssue


@dataclass
class _FakeLLM:
    canned: Any
    captured: dict | None = None

    def complete_with_json(self, **kwargs):
        self.captured = kwargs
        return self.canned


def _user_text(captured: dict) -> str:
    """Flatten messages[0].content (list of text blocks for cache_control)
    back into a single string for substring assertions."""
    content = captured["messages"][0]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def _make_brief(eid):
    return BriefData(
        entity_id=eid,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body="ok"),
    )


def test_no_revision_block_when_no_previous_brief():
    entity = Entity(name="X", type=EntityType.COMPANY)
    canned = _make_brief(entity.id)
    fake = _FakeLLM(canned=canned)
    AuthorAgent().run(
        AuthorInput(entity=entity, ledger=[], estimates=[]),
        PipelineContext(llm_client=fake),
    )
    user_content = _user_text(fake.captured)
    assert "REVISION MODE" not in user_content


def test_revision_block_present_with_previous_brief_and_issues():
    entity = Entity(name="X", type=EntityType.COMPANY)
    canned = _make_brief(entity.id)
    fake = _FakeLLM(canned=canned)
    previous = _make_brief(entity.id)
    issues = [
        ReviewIssue(severity="critical", location="page 1",
                    claim="$1M", issue="No method_id traces to this badge"),
    ]
    AuthorAgent().run(
        AuthorInput(entity=entity, ledger=[], estimates=[],
                    previous_brief=previous, reviewer_issues=issues,
                    iteration=2),
        PipelineContext(llm_client=fake),
    )
    user_content = _user_text(fake.captured)
    assert "REVISION MODE (iteration 2)" in user_content
    assert "No method_id traces to this badge" in user_content
    assert "critical" in user_content


def test_revision_block_absent_when_only_one_of_previous_or_issues():
    """Both previous_brief AND reviewer_issues must be present to trigger
    revision mode. Either one alone is ignored."""
    entity = Entity(name="X", type=EntityType.COMPANY)
    canned = _make_brief(entity.id)
    fake = _FakeLLM(canned=canned)
    previous = _make_brief(entity.id)
    AuthorAgent().run(
        AuthorInput(entity=entity, ledger=[], estimates=[],
                    previous_brief=previous,  # but no issues
                    iteration=2),
        PipelineContext(llm_client=fake),
    )
    user_content = _user_text(fake.captured)
    assert "REVISION MODE" not in user_content
