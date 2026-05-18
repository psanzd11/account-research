"""End-to-end revision loop with a queued fake LLM.

Exercises the real Author → Designer → Reviewer wiring through the
orchestrator. The Designer runs for real (matplotlib + reportlab) and
emits an actual PDF. Only the LLM-bound agents (Author, Reviewer) are
faked, via a small queued LLM that returns pre-built Pydantic objects
in call order.

Scenario:
  iter 1: Author emits a brief with an uncited claim
          → Reviewer flags critical "uncited claim on page 1"
          → orchestrator runs Author again
  iter 2: Author emits a corrected brief
          → Reviewer approves
          → orchestrator returns iterations_used=2, status=approved

NOTE: These tests disable the Round 2 structural-rigor checks
(REVIEWER_RIGOR) at the test fixture level. The canned briefs are minimal
(no industries, no geographic_footprint) and would trip the thin-section
critical injector. The revision-loop contract being tested is the
orchestrator's handling of LLM-driven approval/revision flow, not rigor.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from account_research.agents.base import PipelineContext
from account_research.orchestrator import run_with_revision


@pytest.fixture(autouse=True)
def _disable_rigor_for_revision_loop(monkeypatch):
    """The revision-loop tests use minimal briefs that would trip Reviewer
    rigor (industries<3, no geographic_footprint). Disable rigor here —
    rigor has its own dedicated tests in tests/unit/test_reviewer.py."""
    monkeypatch.setattr("account_research.agents.reviewer._RIGOR_ENABLED", False)
from account_research.schemas.brief import (
    BriefData,
    HeroBadge,
    HeroSection,
    IndustryChip,
    QuickTake,
    StatBlock,
)
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.estimate import Estimate
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)
from account_research.schemas.review import ReviewerReport, ReviewIssue


class _QueuedLLM:
    """Returns one canned object per call, in order. Records the agent name
    of each call so tests can assert the expected sequence."""

    def __init__(self, queue: list[Any]):
        self.queue = list(queue)
        self.agent_log: list[str] = []

    def complete_with_json(self, **kwargs):  # noqa: D401 — signature mirrors LLMClient
        self.agent_log.append(kwargs.get("agent", "?"))
        if not self.queue:
            raise AssertionError(f"FakeLLM queue empty; called from agent={kwargs.get('agent')}")
        return self.queue.pop(0)


def _ev(entity_id: UUID, claim: str) -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=entity_id,
        claim=claim,
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://example.com/p",
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote=claim,
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="verified", method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


def _draft_with_issue(entity_id: UUID, ev_id: UUID) -> BriefData:
    """A brief that has at least one *uncited* element — the trigger the
    Reviewer is supposed to catch on iteration 1."""
    return BriefData(
        entity_id=entity_id,
        hero=HeroSection(
            name="TestCo",
            entity_type=EntityType.COMPANY,
            tagline="A test entity",
            badge=HeroBadge(label="EST. REVENUE", value=None),  # null badge OK
        ),
        quick_take=QuickTake(
            body="TestCo was founded in 2020.",  # shares tokens with raw_quote
            evidence_ids=[ev_id],
        ),
        stats=[
            StatBlock(value="2020", label="FOUNDED", evidence_id=ev_id),
            # Stat without evidence — the issue Reviewer will catch
            StatBlock(value="100+", label="CUSTOMERS", evidence_id=None),
        ],
        industries=[IndustryChip(name="Software", evidence_ids=[ev_id])],
    )


def _draft_corrected(entity_id: UUID, ev_id: UUID) -> BriefData:
    """The Author's iteration-2 redraft: the uncited stat is removed."""
    return BriefData(
        entity_id=entity_id,
        hero=HeroSection(
            name="TestCo",
            entity_type=EntityType.COMPANY,
            tagline="A test entity",
            badge=HeroBadge(label="EST. REVENUE", value=None),
        ),
        quick_take=QuickTake(
            body="TestCo was founded in 2020.",  # shares tokens with raw_quote
            evidence_ids=[ev_id],
        ),
        stats=[
            StatBlock(value="2020", label="FOUNDED", evidence_id=ev_id),
            # Uncited stat dropped per Reviewer's critical issue
        ],
        industries=[IndustryChip(name="Software", evidence_ids=[ev_id])],
    )


def test_revision_loop_recovers_in_two_iterations(tmp_path: Path):
    entity = Entity(name="TestCo", type=EntityType.COMPANY)
    ev = _ev(entity.id, "Founded in 2020.")

    pdf_path = tmp_path / "brief.pdf"

    iter1_brief = _draft_with_issue(entity.id, ev.id)
    iter1_report = ReviewerReport(
        status="revision_required",
        iteration=1,
        pdf_path=str(pdf_path),
        issues=[
            ReviewIssue(
                severity="critical",
                location="page 1, stat block 'CUSTOMERS'",
                claim="100+ CUSTOMERS",
                issue="Stat value is rendered but evidence_id is null — uncited claim.",
                suggested_fix="Drop the stat or add an evidence_id from the ledger.",
            ),
        ],
    )
    iter2_brief = _draft_corrected(entity.id, ev.id)
    iter2_report = ReviewerReport(
        status="approved", iteration=2, pdf_path=str(pdf_path),
    )

    fake = _QueuedLLM([iter1_brief, iter1_report, iter2_brief, iter2_report])
    ctx = PipelineContext(llm_client=fake)

    result = run_with_revision(
        entity=entity, ledger=[ev], estimates=[],
        ctx=ctx, pdf_path=pdf_path, max_iterations=3,
    )

    # Final state
    assert result.iterations_used == 2
    assert result.review.status == "approved"
    assert result.brief is iter2_brief  # corrected brief is what we keep

    # Sequence of agent calls
    assert fake.agent_log == ["author", "reviewer", "author", "reviewer"]

    # The PDF actually got rendered (Designer ran for real)
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 1000


def test_revision_loop_escalates_when_author_keeps_failing(tmp_path: Path):
    entity = Entity(name="TestCo", type=EntityType.COMPANY)
    ev = _ev(entity.id, "Founded in 2020.")
    pdf_path = tmp_path / "brief.pdf"

    bad_brief = _draft_with_issue(entity.id, ev.id)
    bad_report = ReviewerReport(
        status="revision_required",
        iteration=1,  # iteration is forced by ReviewerAgent.run, value here is placeholder
        pdf_path=str(pdf_path),
        issues=[
            ReviewIssue(severity="critical", location="x", claim="y",
                        issue="still uncited"),
        ],
    )

    # 3 author drafts + 3 reviewer reports, all bad
    fake = _QueuedLLM([
        bad_brief, bad_report,
        bad_brief, bad_report,
        bad_brief, bad_report,
    ])
    ctx = PipelineContext(llm_client=fake)

    result = run_with_revision(
        entity=entity, ledger=[ev], estimates=[],
        ctx=ctx, pdf_path=pdf_path, max_iterations=3,
    )

    assert result.iterations_used == 3
    assert result.review.status == "human_review_needed"
    # 3 author + 3 reviewer = 6 LLM calls total
    assert len(fake.agent_log) == 6


def test_revision_loop_first_iteration_approved(tmp_path: Path):
    """Sanity: the happy path still works after all the revision plumbing."""
    entity = Entity(name="TestCo", type=EntityType.COMPANY)
    ev = _ev(entity.id, "Founded in 2020.")
    pdf_path = tmp_path / "brief.pdf"

    good_brief = _draft_corrected(entity.id, ev.id)
    good_report = ReviewerReport(
        status="approved", iteration=1, pdf_path=str(pdf_path),
    )

    fake = _QueuedLLM([good_brief, good_report])
    ctx = PipelineContext(llm_client=fake)

    result = run_with_revision(
        entity=entity, ledger=[ev], estimates=[],
        ctx=ctx, pdf_path=pdf_path, max_iterations=3,
    )

    assert result.iterations_used == 1
    assert result.review.status == "approved"
    assert fake.agent_log == ["author", "reviewer"]
