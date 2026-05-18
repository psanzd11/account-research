"""Tests for the refine module: weakness analyzer + focus prompt builder."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from account_research.refine import (
    HIGH_AGGREGATOR_SHARE_THRESHOLD,
    LOW_TIER1_SHARE_THRESHOLD,
    REQUIRED_CATEGORIES,
    WeaknessProfile,
    analyze_weaknesses,
    build_focus_prompt,
)
from account_research.schemas.entity import Entity, EntityType
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


def _item(
    *,
    entity_id,
    category: EvidenceCategory = EvidenceCategory.COMPANY_FACTS,
    status: str = "verified",
    source_url: str = "https://example.com/page",
    source_type: SourceType = SourceType.OFFICIAL_SITE,
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
) -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        id=uuid4(),
        entity_id=entity_id,
        claim="The company exists.",
        category=category,
        source_url=source_url,
        source_type=source_type,
        raw_quote="The company exists, with offices in three regions.",
        fetched_at=datetime.now(timezone.utc),
        confidence=confidence,
        verification=Verification(
            status=status,  # type: ignore[arg-type]
            method="exact_match",
            checked_at=datetime.now(timezone.utc),
        ),
    )


def _entity() -> Entity:
    return Entity(
        id=uuid4(), name="Acme", type=EntityType.COMPANY,
        primary_url="https://acme.com",
    )


# ---------------------------------------------------------------------------
# analyze_weaknesses
# ---------------------------------------------------------------------------


def test_analyze_empty_ledger_is_thin() -> None:
    profile = analyze_weaknesses([])
    assert profile.is_thin()
    assert profile.verified_count == 0
    assert profile.total_count == 0
    assert profile.verification_rate == 0.0
    # When the ledger is empty, every required category is missing.
    assert set(profile.category_gaps) == set(REQUIRED_CATEGORIES)


def test_analyze_detects_category_gaps() -> None:
    e = _entity()
    items = [
        _item(entity_id=e.id, category=EvidenceCategory.COMPANY_FACTS),
        _item(entity_id=e.id, category=EvidenceCategory.FINANCIAL),
        # Missing: LEADERSHIP, PRODUCTS, CLIENTS, GEOGRAPHY.
    ]
    profile = analyze_weaknesses(items)
    assert not profile.is_thin()
    assert EvidenceCategory.LEADERSHIP in profile.category_gaps
    assert EvidenceCategory.GEOGRAPHY in profile.category_gaps
    assert EvidenceCategory.COMPANY_FACTS not in profile.category_gaps


def test_analyze_tier1_and_aggregator_share() -> None:
    e = _entity()
    items = [
        _item(entity_id=e.id, source_url="https://acme.com/about",
              source_type=SourceType.OFFICIAL_SITE),
        _item(entity_id=e.id, source_url="https://www.sec.gov/Archives/abc",
              source_type=SourceType.SEC_FILING),
        # Aggregator → Tier-3.
        _item(entity_id=e.id, source_url="https://www.crunchbase.com/org/acme",
              source_type=SourceType.AGGREGATOR),
    ]
    profile = analyze_weaknesses(items)
    assert profile.verified_count == 3
    # Tier-1 share = 2/3 over verified items.
    assert profile.tier1_share > 0.6
    assert profile.aggregator_share > 0.3


def test_analyze_dead_and_unverifiable_counts() -> None:
    e = _entity()
    items = [
        _item(entity_id=e.id, status="verified"),
        _item(entity_id=e.id, status="source_dead"),
        _item(entity_id=e.id, status="source_dead"),
        _item(entity_id=e.id, status="unverifiable"),
    ]
    profile = analyze_weaknesses(items)
    assert profile.dead_link_count == 2
    assert profile.unverifiable_count == 1
    assert profile.verified_count == 1
    assert profile.total_count == 4


def test_needs_tier1_push_flag() -> None:
    e = _entity()
    # 1 Tier-1 + 4 aggregator => Tier-1 share 20%, aggregator share 80%.
    items = [
        _item(entity_id=e.id, source_type=SourceType.OFFICIAL_SITE),
        *[_item(entity_id=e.id,
                source_url="https://www.crunchbase.com/x",
                source_type=SourceType.AGGREGATOR) for _ in range(4)],
    ]
    profile = analyze_weaknesses(items)
    assert profile.needs_tier1_push() is True


def test_needs_link_refresh_flag() -> None:
    e = _entity()
    items = [
        _item(entity_id=e.id, status="source_dead"),
        _item(entity_id=e.id, status="verified"),
    ]
    profile = analyze_weaknesses(items)
    assert profile.needs_link_refresh() is True


def test_summary_line_is_one_line() -> None:
    profile = WeaknessProfile(
        verified_count=10, total_count=15,
        verification_rate=0.67, tier_counts={1: 3, 2: 5, 3: 2},
        tier1_share=0.30, aggregator_share=0.20,
        dead_link_count=2, unverifiable_count=3,
        category_gaps=[EvidenceCategory.LEADERSHIP],
    )
    line = profile.summary_line()
    assert "\n" not in line
    assert "verified=10/15" in line
    assert "leadership" in line


# ---------------------------------------------------------------------------
# build_focus_prompt
# ---------------------------------------------------------------------------


def test_focus_prompt_lists_gaps_when_no_override() -> None:
    e = _entity()
    profile = WeaknessProfile(
        category_gaps=[EvidenceCategory.FINANCIAL, EvidenceCategory.LEADERSHIP],
        verified_count=5, total_count=10,
    )
    prompt = build_focus_prompt(e, profile, existing_evidence_ids=[])
    assert "financial:" in prompt.lower()
    assert "leadership:" in prompt.lower()
    assert "products:" not in prompt.lower()


def test_focus_prompt_respects_focus_category_override() -> None:
    e = _entity()
    profile = WeaknessProfile(
        category_gaps=[EvidenceCategory.FINANCIAL, EvidenceCategory.LEADERSHIP],
        verified_count=5, total_count=10,
    )
    prompt = build_focus_prompt(
        e, profile, existing_evidence_ids=[],
        focus_category=EvidenceCategory.GEOGRAPHY,
    )
    # Override means we focus on geography even though it's not a gap.
    assert "geography:" in prompt.lower()
    assert "user-directed focus" in prompt.lower()


def test_focus_prompt_emits_tier1_push_when_flag_on() -> None:
    e = _entity()
    profile = WeaknessProfile(
        tier1_share=0.10, aggregator_share=0.70,
        verified_count=10, total_count=10,
    )
    prompt = build_focus_prompt(e, profile, existing_evidence_ids=[])
    assert "tier-1 push" in prompt.lower()


def test_focus_prompt_omits_tier1_push_when_healthy() -> None:
    e = _entity()
    profile = WeaknessProfile(
        tier1_share=0.55, aggregator_share=0.10,
        verified_count=10, total_count=10,
    )
    prompt = build_focus_prompt(e, profile, existing_evidence_ids=[])
    assert "tier-1 push" not in prompt.lower()


def test_focus_prompt_includes_known_ids_for_dedup() -> None:
    e = _entity()
    profile = WeaknessProfile(verified_count=2, total_count=2)
    ids = [f"id-{i}" for i in range(35)]
    prompt = build_focus_prompt(e, profile, existing_evidence_ids=ids)
    # First few IDs are listed inline.
    assert "id-0" in prompt
    assert "id-29" in prompt
    # Truncated marker for the rest.
    assert "5 more" in prompt


def test_focus_prompt_warns_about_dead_links() -> None:
    e = _entity()
    profile = WeaknessProfile(
        verified_count=5, total_count=10,
        dead_link_count=3, unverifiable_count=2,
    )
    prompt = build_focus_prompt(e, profile, existing_evidence_ids=[])
    assert "link refresh" in prompt.lower()
    assert "3 source" in prompt
