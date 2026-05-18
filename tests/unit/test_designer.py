"""Designer tests: builds a PDF from BriefData; handles None / empty fields."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from account_research.agents.base import PipelineContext
from account_research.agents.designer import DesignerAgent, DesignInput
from account_research.schemas.brief import (
    BriefData,
    HeroBadge,
    HeroSection,
    IndustryChip,
    QuickTake,
    StatBlock,
)
from account_research.schemas.entity import EntityType
from account_research.schemas.evidence import ConfidenceLevel


def _minimal_brief() -> BriefData:
    eid = uuid4()
    return BriefData(
        entity_id=eid,
        hero=HeroSection(
            name="TestCo",
            entity_type=EntityType.COMPANY,
            tagline="A test entity",
            badge=HeroBadge(
                label="EST. REVENUE",
                value="$1-3M",
                unit="USD/yr",
                caveat="Estimated from indirect signals.",
                method_id="consulting_firm_revenue_v1",
                confidence=ConfidenceLevel.LOW,
            ),
        ),
        quick_take=QuickTake(body="Short take.", evidence_ids=[uuid4()]),
    )


def test_minimal_brief_renders(tmp_path: Path):
    brief = _minimal_brief()
    out = tmp_path / "minimal.pdf"
    report = DesignerAgent().run(
        DesignInput(brief=brief, out_path=str(out)), PipelineContext()
    )
    assert out.exists()
    assert out.stat().st_size > 1000  # not empty
    assert report.pdf_path == str(out)
    assert report.badge_was_null is False


def test_null_badge_renders_insufficient_data(tmp_path: Path):
    brief = _minimal_brief()
    # Strip the badge value to trigger INSUFFICIENT DATA path
    brief = brief.model_copy(update={
        "hero": brief.hero.model_copy(update={
            "badge": HeroBadge(label="EST. NET WORTH", value=None)
        })
    })
    out = tmp_path / "null_badge.pdf"
    report = DesignerAgent().run(
        DesignInput(brief=brief, out_path=str(out)), PipelineContext()
    )
    assert out.exists()
    assert report.badge_was_null is True
    assert any("INSUFFICIENT" in w for w in report.warnings)


def test_empty_sections_omitted_not_padded(tmp_path: Path):
    brief = _minimal_brief()
    out = tmp_path / "empty.pdf"
    report = DesignerAgent().run(
        DesignInput(brief=brief, out_path=str(out)), PipelineContext()
    )
    # Sections with empty lists must report rendered=False
    sections_by_name = {s.name: s for s in report.sections}
    assert sections_by_name["industries"].rendered is False
    assert sections_by_name["geographic_footprint"].rendered is False
    assert sections_by_name["scorecard"].rendered is False
    assert sections_by_name["sources"].rendered is False
    # Hero + quick_take always render
    assert sections_by_name["hero"].rendered is True
    assert sections_by_name["quick_take"].rendered is True


def test_partial_brief_renders_no_crash(tmp_path: Path):
    """Real-world case: some sections present, others empty."""
    brief = _minimal_brief()
    brief = brief.model_copy(update={
        "industries": [
            IndustryChip(name="Technology", evidence_ids=[uuid4()]),
            IndustryChip(name="Finance", evidence_ids=[uuid4()]),
        ],
        "stats": [
            StatBlock(value="2020", label="FOUNDED", evidence_id=uuid4()),
            StatBlock(value="50+", label="PROJECTS", evidence_id=uuid4()),
        ],
    })
    out = tmp_path / "partial.pdf"
    DesignerAgent().run(
        DesignInput(brief=brief, out_path=str(out)), PipelineContext()
    )
    assert out.exists()
