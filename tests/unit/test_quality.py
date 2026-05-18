"""Quality metrics over a BriefData + its ledger.

These metrics describe the BRIEF (what shipped in the PDF), not the raw
Researcher output. They answer questions like: "of the citations the reader
sees, how many trace to a verified source?", "how complete is this PDF?".
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from account_research.quality import (
    citation_backing,
    confidence_score,
    ledger_only_score,
    sections_rendered,
    tier1_share_among_cited,
)
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


def _ev(
    *,
    status: str = "verified",
    source_type: SourceType = SourceType.OFFICIAL_SITE,
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
    url: str = "https://example.com/p",
) -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=uuid4(),
        claim="A claim",
        category=EvidenceCategory.COMPANY_FACTS,
        source_url=url,
        source_type=source_type,
        raw_quote="raw quote text",
        fetched_at=datetime.now(timezone.utc),
        confidence=confidence,
        verification=Verification(
            status=status,  # type: ignore[arg-type]
            method="fuzzy_match",
            checked_at=datetime.now(timezone.utc),
            similarity=0.9 if status == "verified" else None,
        ),
    )


def _brief_citing(*evidence_items: VerifiedEvidenceItem) -> BriefData:
    """Build a minimal brief that cites the given evidence items in
    quick_take.evidence_ids."""
    return BriefData(
        entity_id=uuid4(),
        hero=HeroSection(name="TestCo", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(
            body="ok",
            evidence_ids=[ev.id for ev in evidence_items],
        ),
    )


class TestCitationBacking:
    def test_all_verified_cited_returns_100(self):
        items = [_ev(status="verified") for _ in range(3)]
        brief = _brief_citing(*items)
        assert citation_backing(brief, items) == 100.0

    def test_half_verified_returns_50(self):
        verified = _ev(status="verified")
        fallback = _ev(
            status="unverifiable",
            source_type=SourceType.OFFICIAL_SITE,
            confidence=ConfidenceLevel.HIGH,
        )
        brief = _brief_citing(verified, fallback)
        # Author cites 2 items: 1 verified, 1 Tier-1 fallback → 50% verified
        assert citation_backing(brief, [verified, fallback]) == 50.0

    def test_no_citations_returns_100(self):
        """A brief with no citations is vacuously fully-backed (Designer
        will have shown 'Insufficient public data' for empty fields)."""
        brief = BriefData(
            entity_id=uuid4(),
            hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
            quick_take=QuickTake(body="ok"),
        )
        assert citation_backing(brief, []) == 100.0

    def test_unknown_cited_id_does_not_crash(self):
        """A cited evidence_id not in the ledger is treated as 'not verified'."""
        ev = _ev(status="verified")
        brief = _brief_citing(ev)
        ghost = uuid4()
        brief = brief.model_copy(update={
            "quick_take": brief.quick_take.model_copy(update={
                "evidence_ids": [ev.id, ghost],
            }),
        })
        # 1 verified out of 2 cited
        assert citation_backing(brief, [ev]) == 50.0


class TestTier1ShareAmongCited:
    def test_all_official_site_returns_100(self):
        items = [_ev(source_type=SourceType.OFFICIAL_SITE) for _ in range(3)]
        brief = _brief_citing(*items)
        assert tier1_share_among_cited(brief, items) == 100.0

    def test_aggregator_only_returns_0(self):
        item = _ev(
            source_type=SourceType.AGGREGATOR,
            url="https://theorg.com/x",
        )
        brief = _brief_citing(item)
        assert tier1_share_among_cited(brief, [item]) == 0.0

    def test_no_citations_returns_100(self):
        brief = BriefData(
            entity_id=uuid4(),
            hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
            quick_take=QuickTake(body="ok"),
        )
        assert tier1_share_among_cited(brief, []) == 100.0


class TestSectionsRendered:
    def test_only_hero_and_quick_take_populated(self):
        brief = BriefData(
            entity_id=uuid4(),
            hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
            quick_take=QuickTake(body="ok"),
        )
        populated, total = sections_rendered(brief)
        # hero + quick_take = 2 always populated. Total constant.
        assert populated == 2
        assert total == 17

    def test_counts_each_list_section_when_non_empty(self):
        brief = BriefData(
            entity_id=uuid4(),
            hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
            quick_take=QuickTake(body="ok"),
            stats=[StatBlock(value="2020", label="FOUNDED")],
            industries=[IndustryChip(name="Tech", evidence_ids=[uuid4()])],
        )
        populated, _ = sections_rendered(brief)
        assert populated == 4  # hero + quick_take + stats + industries


class TestConfidenceScore:
    def test_full_brief_with_all_citations_verified_is_high(self):
        items = [_ev(status="verified") for _ in range(3)]
        brief = BriefData(
            entity_id=uuid4(),
            hero=HeroSection(
                name="X", entity_type=EntityType.COMPANY,
                badge=HeroBadge(
                    label="EST", value="$1-5M",
                    method_id="consulting_firm_revenue_v1",
                    caveat="Estimated from indirect signals.",
                ),
            ),
            quick_take=QuickTake(body="ok", evidence_ids=[items[0].id]),
            stats=[StatBlock(value="2020", label="FOUNDED")],
            industries=[IndustryChip(name="Tech", evidence_ids=[items[1].id])],
            sources=[],
        )
        score = confidence_score(brief, items)
        # Citation backing 100, tier-1 100, badge w/ caveat 100, sections 4/17
        # → high but not 100 (sections drag it down)
        assert 60.0 <= score <= 90.0

    def test_no_citations_score_is_low(self):
        """An empty brief gets a low score even though 'no claims' is
        vacuously perfect — we want operators to see thin briefs."""
        brief = BriefData(
            entity_id=uuid4(),
            hero=HeroSection(name="X", entity_type=EntityType.COMPANY),
            quick_take=QuickTake(body="ok"),
        )
        score = confidence_score(brief, [])
        # 2/17 sections * 0.25 = ~3%, plus the 3 vacuously-100 metrics weighted
        # to give a baseline. Should be visibly low.
        assert score < 80.0

    def test_score_in_0_100_range(self):
        ev = _ev(status="verified")
        brief = _brief_citing(ev)
        score = confidence_score(brief, [ev])
        assert 0.0 <= score <= 100.0


class _Row:
    """Minimal SQLAlchemy-row shape that ledger_only_score consumes."""

    def __init__(self, status: str, source_type: str = "official_site",
                 source_url: str = "https://example.com/x"):
        self.verification_status = status
        self.source_type = source_type
        self.source_url = source_url


class TestLedgerOnlyScore:
    def test_returns_none_for_empty_ledger(self):
        assert ledger_only_score([]) is None

    def test_returns_zero_when_all_items_source_dead(self):
        rows = [_Row("source_dead") for _ in range(5)]
        # Edge case: ledger exists but every URL is rotten. Score = 0,
        # not None — caller (Library) renders the number, not a dash.
        assert ledger_only_score(rows) == 0.0

    def test_excludes_source_dead_from_denominator(self):
        """100 verified Tier-1 + 200 source_dead should still score high.

        The dead links are URL rot, not a Researcher quality signal.
        Before this fix the formula was `verified / total` and a refine
        that discovered dead links would tank this score.
        """
        rows = ([_Row("verified", "official_site")] * 100
                + [_Row("source_dead")] * 200)
        score = ledger_only_score(rows)
        assert score is not None
        # 100% verified rate + 100% Tier-1 share + max volume → ~100.
        assert score >= 95.0

    def test_unverifiable_counts_against_score(self):
        rows = [_Row("verified", "official_site")] * 5 + [_Row("unverifiable", "aggregator")] * 5
        score = ledger_only_score(rows)
        assert score is not None
        # 50% verified rate × 0.60 = 30, plus partial tier1 + volume → ~50-60.
        assert 40.0 <= score <= 70.0

    def test_score_does_not_drop_when_refine_adds_dead_links(self):
        """Regression for the user-reported bug: refine that finds N dead
        links should NOT make the score worse than the pre-refine state.
        """
        before = [_Row("verified", "official_site")] * 7 + [_Row("unverifiable", "aggregator")] * 3
        score_before = ledger_only_score(before)

        # Refine adds 20 source_dead items.
        after = before + [_Row("source_dead")] * 20
        score_after = ledger_only_score(after)

        assert score_before is not None and score_after is not None
        assert score_after >= score_before - 0.1  # tolerate rounding
