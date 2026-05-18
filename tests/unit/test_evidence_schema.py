"""Truth-table tests for VerifiedEvidenceItem.is_acceptable_for_author.

The Author hard-gate accepts an item iff:
  - verification.status == "verified", OR
  - source_type ∈ {OFFICIAL_SITE, SEC_FILING, GOV_REGISTRY}
    AND confidence == HIGH
    AND verification.status ∈ {"verified", "unverifiable"}
      (source_dead and contradicted are always excluded).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)


def _make_item(
    *,
    status: str,
    source_type: SourceType,
    confidence: ConfidenceLevel,
) -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=uuid4(),
        claim="Founded in 2020 in the Dominican Republic",
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://bwpm.pro/about/",
        source_type=source_type,
        raw_quote="Desde nuestros inicios en 2020",
        fetched_at=datetime.now(timezone.utc),
        confidence=confidence,
        verification=Verification(
            status=status,  # type: ignore[arg-type]
            method="fuzzy_match",
            checked_at=datetime.now(timezone.utc),
            similarity=0.9 if status == "verified" else None,
        ),
    )


TIER1 = (SourceType.OFFICIAL_SITE, SourceType.SEC_FILING, SourceType.GOV_REGISTRY)
NON_TIER1 = (
    SourceType.PRESS,
    SourceType.LINKEDIN,
    SourceType.NEWS,
    SourceType.SOCIAL,
    SourceType.AGGREGATOR,
    SourceType.OTHER,
)


class TestIsAcceptableForAuthor:
    @pytest.mark.parametrize("source_type", list(SourceType))
    @pytest.mark.parametrize(
        "confidence",
        [
            ConfidenceLevel.VERIFIED,
            ConfidenceLevel.HIGH,
            ConfidenceLevel.MEDIUM,
            ConfidenceLevel.LOW,
        ],
    )
    def test_verified_always_acceptable(
        self, source_type: SourceType, confidence: ConfidenceLevel
    ):
        item = _make_item(
            status="verified", source_type=source_type, confidence=confidence
        )
        assert item.is_acceptable_for_author() is True

    @pytest.mark.parametrize("source_type", TIER1)
    def test_unverifiable_tier1_high_is_acceptable(self, source_type: SourceType):
        item = _make_item(
            status="unverifiable",
            source_type=source_type,
            confidence=ConfidenceLevel.HIGH,
        )
        assert item.is_acceptable_for_author() is True

    @pytest.mark.parametrize("source_type", NON_TIER1)
    def test_unverifiable_non_tier1_high_is_rejected(self, source_type: SourceType):
        item = _make_item(
            status="unverifiable",
            source_type=source_type,
            confidence=ConfidenceLevel.HIGH,
        )
        assert item.is_acceptable_for_author() is False

    @pytest.mark.parametrize(
        "confidence",
        [ConfidenceLevel.MEDIUM, ConfidenceLevel.LOW],
    )
    def test_unverifiable_tier1_non_high_is_rejected(
        self, confidence: ConfidenceLevel
    ):
        item = _make_item(
            status="unverifiable",
            source_type=SourceType.OFFICIAL_SITE,
            confidence=confidence,
        )
        assert item.is_acceptable_for_author() is False

    @pytest.mark.parametrize("source_type", TIER1)
    def test_source_dead_tier1_high_is_rejected(self, source_type: SourceType):
        item = _make_item(
            status="source_dead",
            source_type=source_type,
            confidence=ConfidenceLevel.HIGH,
        )
        assert item.is_acceptable_for_author() is False

    @pytest.mark.parametrize("source_type", TIER1)
    def test_contradicted_tier1_high_is_rejected(self, source_type: SourceType):
        """`contradicted` means the page actively disagreed — never acceptable."""
        item = _make_item(
            status="contradicted",
            source_type=source_type,
            confidence=ConfidenceLevel.HIGH,
        )
        assert item.is_acceptable_for_author() is False
