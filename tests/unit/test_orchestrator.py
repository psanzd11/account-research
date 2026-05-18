"""Orchestrator: revision loop terminates correctly across approved /
revision_required / escalation paths."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from account_research.agents.base import PipelineContext
from account_research.orchestrator import run_with_revision
from account_research.schemas.brief import BriefData, HeroSection, QuickTake
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.review import ReviewIssue, ReviewerReport


def _brief(name="X") -> BriefData:
    return BriefData(
        entity_id=uuid4(),
        hero=HeroSection(name=name, entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body="ok"),
    )


def _patch_agents(monkeypatch, *, review_statuses: list[str], pdf_path: Path):
    """Stub Author/Designer/Reviewer to return canned values."""
    call_log = {"author": 0, "designer": 0, "reviewer": 0}

    def author_run(self, input, ctx):  # type: ignore[no-untyped-def]
        call_log["author"] += 1
        return _brief(name=f"X iter{input.iteration}")

    def designer_run(self, input, ctx):
        call_log["designer"] += 1
        from account_research.agents.designer import DesignReport
        return DesignReport(pdf_path=str(input.out_path))

    def reviewer_run(self, input, ctx):
        call_log["reviewer"] += 1
        status = review_statuses[input.iteration - 1]
        issues = []
        if status == "revision_required":
            issues = [ReviewIssue(severity="critical", location="x",
                                  claim="y", issue="missing citation")]
        return ReviewerReport(status=status, iteration=input.iteration,
                              issues=issues, pdf_path=str(pdf_path))

    monkeypatch.setattr("account_research.agents.author.AuthorAgent.run", author_run)
    monkeypatch.setattr("account_research.agents.designer.DesignerAgent.run", designer_run)
    monkeypatch.setattr("account_research.agents.reviewer.ReviewerAgent.run", reviewer_run)
    return call_log


def test_approves_on_first_iteration(monkeypatch, tmp_path):
    pdf = tmp_path / "x.pdf"
    log = _patch_agents(monkeypatch, review_statuses=["approved"], pdf_path=pdf)
    result = run_with_revision(
        entity=Entity(name="X", type=EntityType.COMPANY),
        ledger=[], estimates=[],
        ctx=PipelineContext(),
        pdf_path=pdf, max_iterations=3,
    )
    assert result.review.status == "approved"
    assert result.iterations_used == 1
    assert log["author"] == 1
    assert log["reviewer"] == 1


def test_recovers_on_second_iteration(monkeypatch, tmp_path):
    pdf = tmp_path / "x.pdf"
    log = _patch_agents(
        monkeypatch,
        review_statuses=["revision_required", "approved"],
        pdf_path=pdf,
    )
    result = run_with_revision(
        entity=Entity(name="X", type=EntityType.COMPANY),
        ledger=[], estimates=[],
        ctx=PipelineContext(),
        pdf_path=pdf, max_iterations=3,
    )
    assert result.review.status == "approved"
    assert result.iterations_used == 2
    assert log["author"] == 2
    assert log["reviewer"] == 2


def test_escalates_after_max_iterations(monkeypatch, tmp_path):
    pdf = tmp_path / "x.pdf"
    log = _patch_agents(
        monkeypatch,
        review_statuses=["revision_required"] * 3,
        pdf_path=pdf,
    )
    result = run_with_revision(
        entity=Entity(name="X", type=EntityType.COMPANY),
        ledger=[], estimates=[],
        ctx=PipelineContext(),
        pdf_path=pdf, max_iterations=3,
    )
    assert result.review.status == "human_review_needed"
    assert result.iterations_used == 3
    assert log["author"] == 3
    assert log["reviewer"] == 3


def test_max_iterations_can_be_capped(monkeypatch, tmp_path):
    pdf = tmp_path / "x.pdf"
    log = _patch_agents(
        monkeypatch,
        review_statuses=["revision_required", "revision_required"],
        pdf_path=pdf,
    )
    result = run_with_revision(
        entity=Entity(name="X", type=EntityType.COMPANY),
        ledger=[], estimates=[],
        ctx=PipelineContext(),
        pdf_path=pdf, max_iterations=2,  # capped
    )
    assert result.review.status == "human_review_needed"
    assert result.iterations_used == 2
    assert log["author"] == 2


# ---------------------------------------------------------------------------
# A3 — Designer orphan-evidence fail-fast triggers Author revision
# ---------------------------------------------------------------------------


def test_orphan_evidence_forces_author_revision(monkeypatch, tmp_path):
    """A3: Designer detects an orphan UUID on iter 1 → orchestrator skips
    Reviewer, loops back to Author with a synthetic critical issue, second
    iter renders cleanly and Reviewer approves."""
    from account_research.agents.base import OrphanEvidenceError
    from account_research.agents.designer import DesignReport

    pdf = tmp_path / "x.pdf"
    call_log = {"author": 0, "designer": 0, "reviewer": 0}

    def author_run(self, payload, ctx):
        call_log["author"] += 1
        # iter 1: brief cites a random UUID (orphan); iter 2: clean brief.
        if call_log["author"] == 1:
            return BriefData(
                entity_id=payload.entity.id,
                hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
                quick_take=QuickTake(body="ok", evidence_ids=[uuid4()]),
            )
        return BriefData(
            entity_id=payload.entity.id,
            hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
            quick_take=QuickTake(body="ok"),
        )

    def designer_run(self, payload, ctx):
        call_log["designer"] += 1
        ledger_ids = {e.id for e in payload.ledger}
        orphans = payload.brief.all_evidence_ids() - ledger_ids
        if orphans:
            raise OrphanEvidenceError(orphans)
        return DesignReport(pdf_path=str(payload.out_path))

    def reviewer_run(self, payload, ctx):
        call_log["reviewer"] += 1
        return ReviewerReport(
            status="approved", iteration=payload.iteration, issues=[],
            pdf_path=str(pdf),
        )

    monkeypatch.setattr("account_research.agents.author.AuthorAgent.run", author_run)
    monkeypatch.setattr("account_research.agents.designer.DesignerAgent.run", designer_run)
    monkeypatch.setattr("account_research.agents.reviewer.ReviewerAgent.run", reviewer_run)

    result = run_with_revision(
        entity=Entity(name="X", type=EntityType.COMPANY),
        ledger=[], estimates=[],
        ctx=PipelineContext(),
        pdf_path=pdf, max_iterations=3,
    )
    assert call_log["author"] == 2
    assert call_log["designer"] == 2
    assert call_log["reviewer"] == 1
    assert result.review.status == "approved"
    assert result.iterations_used == 2


def test_orphan_evidence_on_final_iter_escalates(monkeypatch, tmp_path):
    """A3: orphan on the final iteration escalates to human review."""
    from account_research.agents.base import OrphanEvidenceError
    from account_research.agents.designer import DesignReport

    pdf = tmp_path / "x.pdf"

    def author_run(self, payload, ctx):
        return BriefData(
            entity_id=payload.entity.id,
            hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
            quick_take=QuickTake(body="ok", evidence_ids=[uuid4()]),
        )

    def designer_run(self, payload, ctx):
        ledger_ids = {e.id for e in payload.ledger}
        orphans = payload.brief.all_evidence_ids() - ledger_ids
        if orphans:
            raise OrphanEvidenceError(orphans)
        return DesignReport(pdf_path=str(payload.out_path))

    def reviewer_run(self, payload, ctx):
        raise AssertionError("reviewer must not run when designer rejects")

    monkeypatch.setattr("account_research.agents.author.AuthorAgent.run", author_run)
    monkeypatch.setattr("account_research.agents.designer.DesignerAgent.run", designer_run)
    monkeypatch.setattr("account_research.agents.reviewer.ReviewerAgent.run", reviewer_run)

    result = run_with_revision(
        entity=Entity(name="X", type=EntityType.COMPANY),
        ledger=[], estimates=[],
        ctx=PipelineContext(),
        pdf_path=pdf, max_iterations=2,
    )
    assert result.review.status == "human_review_needed"
    assert result.iterations_used == 2
