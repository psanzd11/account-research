"""Tests for the Author-failure fallback: BriefData.skeleton + orchestrator.

Pipeline rule (memory: always-produce-pdf): the pipeline NEVER halts when
the Author crashes — it falls back to a skeleton brief (or the prior one
in refine mode) so a PDF always ships. The score reflects the degradation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from account_research.agents.author import AuthorAgent
from account_research.agents.base import PipelineContext
from account_research.orchestrator import (
    OrchestratorResult,
    _human_review_needed,
    run_with_revision,
)
from account_research.schemas.brief import BriefData, HeroSection, QuickTake
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.estimate import (
    ConfidenceLevel as EstimateConfidence,
    Estimate,
)
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(name: str = "Acme") -> Entity:
    return Entity(
        id=uuid4(), name=name, type=EntityType.COMPANY,
        primary_url="https://acme.com",
    )


def _vitem(entity_id, status: str = "verified") -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        id=uuid4(),
        entity_id=entity_id,
        claim="Sample claim about the entity",
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://example.com/page",
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote="Sample claim about the entity, captured verbatim.",
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status=status,  # type: ignore[arg-type]
            method="exact_match",
            checked_at=datetime.now(timezone.utc),
        ),
    )


def _estimate(entity_id=None) -> Estimate:
    return Estimate(
        entity_id=entity_id or uuid4(),
        applies_to="company",
        method_id="consulting_firm_revenue_v1",
        metric="revenue",
        value_range="$1M-$5M",
        unit="USD/yr",
        confidence=EstimateConfidence.MEDIUM,
        caveat_text="Estimated from team size + project count.",
        assumptions=["12 FTE", "10 projects/yr"],
        signals_used=[],
    )


# ---------------------------------------------------------------------------
# BriefData.skeleton
# ---------------------------------------------------------------------------


def test_skeleton_renders_with_zero_inputs() -> None:
    e = _entity()
    brief = BriefData.skeleton(e, ledger=[], estimates=[])
    assert isinstance(brief, BriefData)
    assert brief.entity_id == e.id
    assert brief.hero.name == "Acme"
    assert brief.hero.entity_type == EntityType.COMPANY
    assert brief.hero.badge is None  # no estimate → no badge
    assert "Insufficient public data" in brief.quick_take.body
    assert brief.quick_take.evidence_ids == []
    assert brief.contacts == []
    assert brief.industries == []


def test_skeleton_badge_uses_first_estimate() -> None:
    e = _entity()
    est = _estimate()
    brief = BriefData.skeleton(e, ledger=[], estimates=[est])
    assert brief.hero.badge is not None
    assert brief.hero.badge.value == "$1M-$5M"
    assert brief.hero.badge.unit == "USD/yr"
    assert brief.hero.badge.caveat == "Estimated from team size + project count."
    assert brief.hero.badge.method_id == "consulting_firm_revenue_v1"


def test_skeleton_confidence_report_counts_ledger_status() -> None:
    e = _entity()
    ledger = [
        _vitem(e.id, "verified"),
        _vitem(e.id, "verified"),
        _vitem(e.id, "unverifiable"),
        _vitem(e.id, "source_dead"),
    ]
    brief = BriefData.skeleton(e, ledger=ledger, estimates=[])
    cr = brief.confidence_report
    assert cr is not None
    assert cr.total_facts == 4
    assert cr.verified == 2
    assert cr.unverifiable == 1
    assert cr.source_dead == 1
    assert cr.estimated == 0


def test_skeleton_renders_to_pdf() -> None:
    """Designer should accept a skeleton without errors — smoke check."""
    from account_research.designer.pdf_builder import build_brief

    e = _entity()
    brief = BriefData.skeleton(e, ledger=[], estimates=[])
    out = Path("outputs") / f"test_skeleton_{uuid4().hex[:8]}.pdf"
    try:
        result = build_brief(brief, out)
        assert result.exists()
        # Should be a tiny PDF — single page with the hero + quick-take.
        assert result.stat().st_size > 1000  # ReportLab overhead alone is > 1KB
    finally:
        if out.exists():
            out.unlink()


# ---------------------------------------------------------------------------
# Orchestrator fallback
# ---------------------------------------------------------------------------


class _FakeLogger:
    def info(self, *a, **kw): pass  # noqa: D401, ANN003
    def warning(self, *a, **kw): pass  # noqa: D401, ANN003


def _ctx() -> PipelineContext:
    return PipelineContext(
        run_id=uuid4(), session=None, llm_client=None,  # type: ignore[arg-type]
        logger=_FakeLogger(),  # type: ignore[arg-type]
    )


def test_human_review_needed_helper() -> None:
    pdf = Path("outputs/dummy.pdf")
    r = _human_review_needed(pdf, iteration=2)
    assert r.status == "human_review_needed"
    assert r.iteration == 2
    assert r.issues == []
    assert r.pdf_path == str(pdf)


def test_human_review_needed_clamps_iteration_to_max_4() -> None:
    r = _human_review_needed(Path("x.pdf"), iteration=99)
    # ReviewerReport.iteration has le=4 — must not raise.
    assert r.iteration == 4


def test_orchestrator_falls_back_to_skeleton_on_iter1_author_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Iter 1 Author crash → skeleton brief + Designer still runs + return."""
    from account_research import orchestrator as orch_mod

    def _explode(self, payload, ctx):  # noqa: ARG001
        raise RuntimeError("synthetic Author crash")

    monkeypatch.setattr(AuthorAgent, "run", _explode)

    rendered: list[BriefData] = []

    class _FakeDesigner:
        def run(self, payload, ctx):  # noqa: ARG002
            rendered.append(payload.brief)
            return type("R", (), dict(warnings=[]))()

    monkeypatch.setattr(orch_mod, "DesignerAgent", lambda: _FakeDesigner())

    e = _entity()
    pdf_path = tmp_path / "out.pdf"

    result = run_with_revision(
        entity=e, ledger=[], estimates=[],
        ctx=_ctx(), pdf_path=pdf_path,
        max_iterations=3,
    )

    # Skeleton brief was rendered.
    assert len(rendered) == 1
    assert "Insufficient public data" in rendered[0].quick_take.body
    # Result reports the fallback synthetic Reviewer state.
    assert result.review.status == "human_review_needed"
    assert result.iterations_used == 1
    assert result.brief is rendered[0]


def test_orchestrator_uses_prior_brief_when_author_crashes_on_iter1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """In refine flow: prior brief from disk wins over skeleton."""
    from account_research import orchestrator as orch_mod

    monkeypatch.setattr(
        AuthorAgent, "run",
        lambda self, payload, ctx: (_ for _ in ()).throw(RuntimeError("crash")),
    )

    rendered: list[BriefData] = []

    class _FakeDesigner:
        def run(self, payload, ctx):  # noqa: ARG002
            rendered.append(payload.brief)
            return type("R", (), dict(warnings=[]))()

    monkeypatch.setattr(orch_mod, "DesignerAgent", lambda: _FakeDesigner())

    e = _entity()
    prior = BriefData(
        entity_id=e.id,
        hero=HeroSection(name="Acme", entity_type=EntityType.COMPANY,
                         tagline="kept from before"),
        quick_take=QuickTake(body="Previous good content."),
    )

    result = run_with_revision(
        entity=e, ledger=[], estimates=[],
        ctx=_ctx(), pdf_path=tmp_path / "out.pdf",
        max_iterations=3,
        prior_brief_fallback=prior,
    )

    assert len(rendered) == 1
    # Rendered with the prior brief, NOT a skeleton.
    assert rendered[0].quick_take.body == "Previous good content."
    assert rendered[0].hero.tagline == "kept from before"
    assert result.brief.hero.tagline == "kept from before"
    assert result.review.status == "human_review_needed"
