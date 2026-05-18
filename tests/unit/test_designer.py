"""Designer tests: builds a PDF from BriefData; handles None / empty fields."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from account_research.agents.base import OrphanEvidenceError, PipelineContext
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
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)


def _ev(entity_id) -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=entity_id,
        claim="some claim",
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://example.com/p",
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote="some claim",
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="verified", method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


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


# ---------------------------------------------------------------------------
# A3 — orphan evidence_id fail-fast
# ---------------------------------------------------------------------------


def test_orphan_evidence_id_raises_orphan_error(tmp_path: Path):
    """A3: a brief whose quick_take cites an evidence_id absent from the
    ledger must raise OrphanEvidenceError before any PDF is written."""
    brief = _minimal_brief()  # quick_take.evidence_ids has one random UUID
    real_ev = _ev(brief.entity_id)
    out = tmp_path / "should_not_be_written.pdf"

    with pytest.raises(OrphanEvidenceError) as exc_info:
        DesignerAgent().run(
            DesignInput(
                brief=brief,
                out_path=str(out),
                ledger=[real_ev],  # quick_take cites a UUID NOT in the ledger
            ),
            PipelineContext(),
        )
    # PDF must not be left behind
    assert not out.exists()
    # The orphan set has exactly the fabricated UUID
    assert len(exc_info.value.orphans) == 1


def test_all_cited_ids_in_ledger_renders_ok(tmp_path: Path):
    """A3: when every cited evidence_id has a matching ledger item, the
    Designer behaves as before."""
    entity_id = uuid4()
    ev = _ev(entity_id)
    brief = BriefData(
        entity_id=entity_id,
        hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body="ok", evidence_ids=[ev.id]),
    )
    out = tmp_path / "valid.pdf"
    DesignerAgent().run(
        DesignInput(brief=brief, out_path=str(out), ledger=[ev]),
        PipelineContext(),
    )
    assert out.exists()


def test_empty_ledger_skips_orphan_check(tmp_path: Path):
    """A3: when no ledger is provided (e.g. CLI `design` subcommand loading
    a hand-edited brief), the orphan check is skipped — backwards-compat
    with the loose-render flow."""
    brief = _minimal_brief()  # cites a random UUID
    out = tmp_path / "no_ledger.pdf"
    DesignerAgent().run(
        DesignInput(brief=brief, out_path=str(out)),  # no ledger arg
        PipelineContext(),
    )
    assert out.exists()
