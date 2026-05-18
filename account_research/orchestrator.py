"""Orchestrator — Author ↔ Designer ↔ Reviewer revision loop.

SPEC §3 + §4.7. Max 3 iterations. If the Reviewer still reports
revision_required after iteration 3, the status is overridden to
human_review_needed and the run halts.

Each iteration spends ~1 Author (Opus 4.7) + 1 Reviewer (Opus 4.7) call,
roughly $3-5 of API. Worst-case 3 iterations costs ~$9-15. The Designer
is free (no LLM).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from account_research._progress import emit as progress
from account_research.agents.author import AuthorAgent, AuthorInput
from account_research.agents.base import PipelineContext
from account_research.agents.designer import DesignerAgent, DesignInput
from account_research.agents.reviewer import ReviewInput, ReviewerAgent
from account_research.schemas.brief import BriefData, ContactItem
from account_research.schemas.entity import Entity
from account_research.schemas.estimate import Estimate
from account_research.schemas.evidence import VerifiedEvidenceItem
from account_research.schemas.review import ReviewerReport

MAX_ITERATIONS = 3


@dataclass
class OrchestratorResult:
    brief: BriefData
    pdf_path: Path
    review: ReviewerReport
    iterations_used: int
    author_failed: bool = False
    """True when Author crashed and the brief came from a fallback
    (skeleton or prior brief on disk). The CLI surfaces this via the
    pipeline_run.status string so the Library UI can warn the user."""


def _human_review_needed(pdf_path: Path, iteration: int) -> ReviewerReport:
    """Synthetic Reviewer report used when the Author crashed and the
    fallback path bypasses the real Reviewer. Surfaces in the CLI summary
    and Library page so the user knows the brief came from a fallback.
    """
    return ReviewerReport(
        status="human_review_needed",
        iteration=min(iteration, 4),
        issues=[],
        pdf_path=str(pdf_path),
    )


def run_with_revision(
    *,
    entity: Entity,
    ledger: list[VerifiedEvidenceItem],
    estimates: list[Estimate],
    ctx: PipelineContext,
    pdf_path: Path,
    max_iterations: int = MAX_ITERATIONS,
    use_vision: bool = False,
    contacts: list[ContactItem] | None = None,
    prior_brief_fallback: BriefData | None = None,
) -> OrchestratorResult:
    """Run Author → Designer → Reviewer up to `max_iterations` times.

    On each revision the previous BriefData and Reviewer issues are fed back
    into the Author so it can fix critical violations. The function returns
    as soon as the Reviewer returns `approved`, or after max_iterations with
    `human_review_needed`.
    """
    previous_brief: BriefData | None = None
    previous_weak: list[dict] = []
    issues = []  # type: list
    brief: BriefData | None = None
    review: ReviewerReport | None = None

    for iteration in range(1, max_iterations + 1):
        ctx.iteration = iteration
        ctx.logger.info("Orchestrator: starting iteration %d/%d", iteration, max_iterations)
        iter_tag = (iteration, max_iterations)

        # ---- Author (with skeleton/previous-brief fallback on crash) ----
        progress("author", "start", iter=iter_tag)
        author_failed = False
        try:
            author_input = AuthorInput(
                entity=entity, ledger=ledger, estimates=estimates,
                previous_brief=previous_brief, reviewer_issues=issues,
                previous_weak_citations=previous_weak,
                iteration=iteration,
            )
            author = AuthorAgent()
            brief = author.run(author_input, ctx)
            weak_citations = list(author.last_weak_citations)
            if contacts:
                brief = brief.model_copy(update={"contacts": contacts})
            progress("author", "done", iter=iter_tag)
        except Exception as exc:  # noqa: BLE001
            author_failed = True
            progress("author", "failed", iter=iter_tag)
            ctx.logger.warning(
                "Orchestrator: Author failed on iter %d (%s) — falling back. "
                "Pipeline rule: always produce a PDF.",
                iteration, exc,
            )
            if previous_brief is not None:
                # We already have a vetted brief + rendered PDF from iter N-1.
                # Return it as the final result — no point continuing the loop.
                return OrchestratorResult(
                    brief=previous_brief, pdf_path=pdf_path,
                    review=_human_review_needed(pdf_path, iteration),
                    iterations_used=iteration,
                    author_failed=True,
                )
            # First iteration crash — prefer a prior brief from disk
            # (refine flow), else synthesise a skeleton.
            if prior_brief_fallback is not None:
                brief = prior_brief_fallback
                ctx.logger.info(
                    "Orchestrator: using prior brief from disk as fallback",
                )
            else:
                brief = BriefData.skeleton(entity, ledger, estimates)
            if contacts:
                brief = brief.model_copy(update={"contacts": contacts})
            weak_citations = []

        # ---- Designer ----
        progress("designer", "start", iter=iter_tag)
        try:
            DesignerAgent().run(
                DesignInput(brief=brief, out_path=str(pdf_path)), ctx
            )
            progress("designer", "done", iter=iter_tag)
        except Exception:
            progress("designer", "failed", iter=iter_tag)
            raise

        # If Author fell back, Reviewer has nothing useful to evaluate.
        # Mark it skipped and return — the score downstream will reflect
        # the degraded brief.
        if author_failed:
            progress("reviewer", "skipped")
            return OrchestratorResult(
                brief=brief, pdf_path=pdf_path,
                review=_human_review_needed(pdf_path, iteration),
                iterations_used=iteration,
                author_failed=True,
            )

        # ---- Reviewer ----
        progress("reviewer", "start", iter=iter_tag)
        try:
            review = ReviewerAgent().run(
                ReviewInput(
                    brief=brief, ledger=ledger, estimates=estimates,
                    pdf_path=str(pdf_path), iteration=iteration,
                    use_vision=use_vision,
                    weak_citations=weak_citations,
                ),
                ctx,
            )
            progress("reviewer", "done", iter=iter_tag, info=review.status)
        except Exception:
            progress("reviewer", "failed", iter=iter_tag)
            raise

        ctx.logger.info(
            "Orchestrator: iteration %d → reviewer status=%s with %d issue(s)",
            iteration, review.status, len(review.issues),
        )

        if review.status == "approved":
            return OrchestratorResult(
                brief=brief, pdf_path=pdf_path, review=review,
                iterations_used=iteration,
            )

        if iteration == max_iterations:
            # Escalate per SPEC §4.7
            review = review.model_copy(update={"status": "human_review_needed"})
            ctx.logger.warning(
                "Orchestrator: max iterations reached, escalating to human review",
            )
            return OrchestratorResult(
                brief=brief, pdf_path=pdf_path, review=review,
                iterations_used=iteration,
            )

        previous_brief = brief
        previous_weak = weak_citations
        issues = list(review.issues)

    # Unreachable — the loop always returns. Type narrowing only.
    assert brief is not None and review is not None
    return OrchestratorResult(
        brief=brief, pdf_path=pdf_path, review=review,
        iterations_used=max_iterations,
    )
