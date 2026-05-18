"""Sprint 3.1 — programmatic walk JSON↔PDF.

For each leaf BriefData field that the Designer renders into the PDF, the
walk asserts the value appears in the extracted PDF text (after Unicode +
whitespace normalization). Missing values become `designer_dropped_field`
critical issues — caught before the LLM-based Reviewer pass.

The sentinel "Insufficient public data" is treated as an intentional empty
and never raises a dropped-field issue.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from account_research.agents.reviewer_walk import walk_brief_against_pdf
from account_research.schemas.brief import (
    BriefData,
    GeographicLocation,
    HeroBadge,
    HeroSection,
    IndustryChip,
    QuickTake,
    SourceRef,
    StatBlock,
)
from account_research.schemas.entity import EntityType


def _brief(**overrides) -> BriefData:
    eid = uuid4()
    base = dict(
        entity_id=eid,
        hero=HeroSection(
            name="TestCo",
            entity_type=EntityType.COMPANY,
            badge=HeroBadge(
                label="EST. REVENUE",
                value="$2-5M",
                unit="USD/yr",
                caveat="Estimated from indirect signals.",
                method_id="consulting_firm_revenue_v1",
            ),
        ),
        quick_take=QuickTake(body="Some quick take.", evidence_ids=[uuid4()]),
        stats=[
            StatBlock(value="2020", label="FOUNDED", evidence_id=uuid4()),
            StatBlock(value="50+", label="PROJECTS", evidence_id=uuid4()),
        ],
        industries=[
            IndustryChip(name="Technology", evidence_ids=[uuid4()]),
            IndustryChip(name="Retail", evidence_ids=[uuid4()]),
        ],
        geographic_footprint=[
            GeographicLocation(
                location="Dominican Republic", evidence_ids=[uuid4()]
            ),
        ],
        sources=[
            SourceRef(
                title="Founder bio on official site",
                url="https://example.com/about",
                source_type="official_site",
                evidence_ids=[uuid4()],
            ),
        ],
    )
    base.update(overrides)
    return BriefData(**base)


def test_no_issues_when_every_value_appears():
    brief = _brief()
    pdf_text = (
        "TestCo\n"
        "EST. REVENUE  $2-5M  USD/yr\n"
        "Estimated from indirect signals.\n"
        "2020  FOUNDED\n"
        "50+  PROJECTS\n"
        "Technology  Retail\n"
        "Dominican Republic\n"
        "Founder bio on official site  https://example.com/about"
    )
    issues = walk_brief_against_pdf(brief, pdf_text)
    assert issues == []


def test_flags_dropped_stat_value():
    """Stats[1] value '50+' missing from PDF → critical dropped_field."""
    brief = _brief()
    pdf_text = (
        "TestCo  $2-5M  USD/yr  Estimated from indirect signals.\n"
        "2020 FOUNDED  Technology Retail  Dominican Republic\n"
        "Founder bio on official site"
        # '50+' deliberately missing
    )
    issues = walk_brief_against_pdf(brief, pdf_text)
    assert any(i.severity == "critical" for i in issues)
    locations = [i.location for i in issues]
    assert any("stats[1]" in loc for loc in locations)


def test_flags_dropped_industry_chip():
    brief = _brief()
    pdf_text = (
        "TestCo $2-5M USD/yr Estimated from indirect signals. "
        "2020 FOUNDED 50+ PROJECTS Technology "  # Retail dropped
        "Dominican Republic Founder bio on official site"
    )
    issues = walk_brief_against_pdf(brief, pdf_text)
    assert any("industries" in i.location for i in issues)


def test_flags_dropped_caveat():
    """Estimate caveat must be rendered next to the badge value."""
    brief = _brief()
    pdf_text = (
        "TestCo $2-5M USD/yr "  # caveat dropped
        "2020 FOUNDED 50+ PROJECTS Technology Retail "
        "Dominican Republic Founder bio on official site"
    )
    issues = walk_brief_against_pdf(brief, pdf_text)
    assert any("badge.caveat" in i.location for i in issues)


def test_insufficient_sentinel_skips_value_check():
    """A stat with 'Insufficient public data' is intentionally empty —
    no critical issue even though the value isn't in the PDF as-is."""
    brief = _brief(
        stats=[
            StatBlock(
                value="Insufficient public data",
                label="REVENUE",
                evidence_id=None,
            ),
        ],
    )
    pdf_text = (
        "TestCo $2-5M USD/yr Estimated from indirect signals. "
        # No 'Insufficient public data' string visible — and that's fine
        "Technology Retail Dominican Republic Founder bio on official site"
    )
    issues = walk_brief_against_pdf(brief, pdf_text)
    # No stats-related issue should fire
    assert not any("stats[" in i.location for i in issues)


def test_normalizes_en_dash_to_hyphen():
    """Badge value '$2–5M' (en-dash) appearing as '$2-5M' (hyphen) in PDF
    text must still match."""
    brief = _brief()
    brief = brief.model_copy(update={
        "hero": brief.hero.model_copy(update={
            "badge": brief.hero.badge.model_copy(update={"value": "$2–5M"}),
        }),
    })
    pdf_text = (
        "TestCo EST. REVENUE $2-5M USD/yr Estimated from indirect signals. "
        "2020 FOUNDED 50+ PROJECTS Technology Retail Dominican Republic "
        "Founder bio on official site"
    )
    issues = walk_brief_against_pdf(brief, pdf_text)
    assert not any("badge.value" in i.location for i in issues)
