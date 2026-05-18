"""Source authority classifier — tier_of, is_aggregator, cap."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from account_research.agents.source_authority import (
    aggregator_share,
    cap_aggregators_if_exceeded,
    is_aggregator,
    tier1_count_per_category,
    tier_breakdown,
    tier_of,
)
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)


def _vev(url: str, *, source_type=SourceType.OFFICIAL_SITE,
         category=EvidenceCategory.COMPANY_FACTS,
         confidence=ConfidenceLevel.HIGH,
         status="verified") -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=uuid4(),
        claim="claim",
        category=category,
        source_url=url,
        source_type=source_type,
        raw_quote="quote",
        fetched_at=datetime.now(timezone.utc),
        confidence=confidence,
        verification=Verification(
            status=status, method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


def test_sec_filing_is_tier_1():
    assert tier_of("https://www.sec.gov/edgar/x", SourceType.SEC_FILING) == 1


def test_gov_registry_is_tier_1():
    assert tier_of("https://disclosures.ifc.org/x", SourceType.GOV_REGISTRY) == 1
    assert tier_of("https://disclosures.ifc.org/x", SourceType.OTHER) == 1


def test_official_site_is_tier_1():
    assert tier_of("https://acme.com/about", SourceType.OFFICIAL_SITE) == 1


def test_mainstream_press_tier_1():
    assert tier_of("https://www.bloomberg.com/x", SourceType.NEWS) == 1
    assert tier_of("https://www.nytimes.com/x", SourceType.PRESS) == 1
    assert tier_of("https://expansion.mx/foo", SourceType.NEWS) == 1


def test_aggregator_is_tier_3():
    assert tier_of("https://theorg.com/people/x", SourceType.AGGREGATOR) == 3
    assert tier_of("https://rocketreach.co/x", SourceType.OTHER) == 3
    assert tier_of("https://www.zoominfo.com/x", SourceType.OTHER) == 3
    assert tier_of("https://contactout.com/x", SourceType.OTHER) == 3


def test_wikipedia_is_tier_2():
    assert tier_of("https://en.wikipedia.org/wiki/Foo", SourceType.OTHER) == 2
    assert tier_of("https://es.wikipedia.org/wiki/Foo", SourceType.OTHER) == 2


def test_linkedin_is_tier_2():
    assert tier_of("https://www.linkedin.com/in/x", SourceType.LINKEDIN) == 2


def test_is_aggregator_known_hosts():
    assert is_aggregator("https://theorg.com/x")
    assert is_aggregator("https://www.rocketreach.co/x")
    assert not is_aggregator("https://acme.com")
    assert not is_aggregator("https://en.wikipedia.org/wiki/x")


def test_aggregator_share_calculation():
    items = [
        _vev("https://theorg.com/x", source_type=SourceType.AGGREGATOR),
        _vev("https://theorg.com/y", source_type=SourceType.AGGREGATOR),
        _vev("https://acme.com", source_type=SourceType.OFFICIAL_SITE),
        _vev("https://www.bloomberg.com/x", source_type=SourceType.NEWS),
    ]
    assert aggregator_share(items) == 0.5


def test_tier_breakdown():
    items = [
        _vev("https://www.sec.gov/x", source_type=SourceType.SEC_FILING),
        _vev("https://www.nytimes.com/x", source_type=SourceType.NEWS),
        _vev("https://en.wikipedia.org/x", source_type=SourceType.OTHER),
        _vev("https://theorg.com/x", source_type=SourceType.AGGREGATOR),
        _vev("https://rocketreach.co/x", source_type=SourceType.AGGREGATOR),
    ]
    b = tier_breakdown(items)
    assert b[1] == 2  # SEC + NYT
    assert b[2] == 1  # Wikipedia
    assert b[3] == 2  # 2 aggregators


def test_tier1_count_per_category():
    items = [
        _vev("https://www.sec.gov/x", source_type=SourceType.SEC_FILING,
             category=EvidenceCategory.FINANCIAL),
        _vev("https://www.bloomberg.com/x", source_type=SourceType.NEWS,
             category=EvidenceCategory.LEADERSHIP),
        _vev("https://en.wikipedia.org/x", source_type=SourceType.OTHER,
             category=EvidenceCategory.LEADERSHIP),
    ]
    counts = tier1_count_per_category(items)
    assert counts.get("financial") == 1
    assert counts.get("leadership") == 1  # Wikipedia is tier 2, not counted
    assert "products" not in counts


def test_cap_aggregators_when_exceeded():
    """8 items: 5 aggregator + 3 official → 62.5% aggregator. Cap at 25% → keep ≤2 aggregators."""
    items = []
    for i in range(5):
        items.append(_vev(f"https://theorg.com/{i}",
                          source_type=SourceType.AGGREGATOR,
                          confidence=ConfidenceLevel.LOW))
    for i in range(3):
        items.append(_vev(f"https://acme.com/{i}",
                          source_type=SourceType.OFFICIAL_SITE,
                          confidence=ConfidenceLevel.HIGH))

    kept, dropped = cap_aggregators_if_exceeded(items, max_share=0.25)
    # 8 items × 0.25 = 2 → keep 2 aggregator items
    assert dropped == 3
    remaining_aggs = sum(1 for ev in kept if is_aggregator(str(ev.source_url)))
    assert remaining_aggs == 2


def test_cap_skipped_when_under_threshold():
    items = [
        _vev("https://theorg.com/x", source_type=SourceType.AGGREGATOR),
        _vev("https://acme.com", source_type=SourceType.OFFICIAL_SITE),
        _vev("https://www.bloomberg.com/x", source_type=SourceType.NEWS),
        _vev("https://www.nytimes.com/x", source_type=SourceType.NEWS),
    ]
    # 25% aggregator → at threshold → no cap
    kept, dropped = cap_aggregators_if_exceeded(items, max_share=0.25)
    assert dropped == 0
    assert kept == items


def test_cap_disabled_via_env(monkeypatch):
    monkeypatch.setenv("SOURCE_AUTHORITY_GATING", "0")
    # Reimport to pick up new env
    import importlib
    from account_research.agents import source_authority
    importlib.reload(source_authority)

    items = [_vev(f"https://theorg.com/{i}",
                  source_type=SourceType.AGGREGATOR) for i in range(5)]
    items.append(_vev("https://acme.com", source_type=SourceType.OFFICIAL_SITE))

    kept, dropped = source_authority.cap_aggregators_if_exceeded(items)
    assert dropped == 0
    assert kept == items

    # Reset for downstream tests
    monkeypatch.setenv("SOURCE_AUTHORITY_GATING", "1")
    importlib.reload(source_authority)
