"""Multi-source corroboration (Round 2 / G2)."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from account_research.agents.corroboration import (
    apply_corroboration_to_recipe_signals,
    compute_corroboration,
    corroboration_stats,
)
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)


def _vev(claim: str, url: str, *,
         category=EvidenceCategory.COMPANY_FACTS,
         status="verified") -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=uuid4(),
        claim=claim,
        category=category,
        source_url=url,
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote=claim,
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status=status, method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


def test_singleton_items_get_count_1():
    items = [
        _vev("Founded in 1998 in Buenos Aires", "https://a.example/"),
        _vev("CEO is Marcos Galperin", "https://b.example/"),
    ]
    counts = compute_corroboration(items)
    assert all(c == 1 for c in counts.values())


def test_same_claim_different_hosts_corroborates():
    """Two items with similar claims from different hosts → group size 2."""
    items = [
        _vev("Founded 1999 by Marcos Galperin in Buenos Aires Argentina",
             "https://www.bloomberg.com/x"),
        _vev("Founded 1999 by Galperin Buenos Aires Argentina",
             "https://en.wikipedia.org/y"),
    ]
    counts = compute_corroboration(items)
    assert all(c == 2 for c in counts.values())


def test_same_host_does_not_corroborate():
    """Two items from the SAME host don't corroborate (not independent)."""
    items = [
        _vev("Founded 1999 by Galperin in Buenos Aires Argentina",
             "https://www.bloomberg.com/x"),
        _vev("Founded 1999 by Galperin Buenos Aires Argentina",
             "https://www.bloomberg.com/y"),
    ]
    counts = compute_corroboration(items)
    assert all(c == 1 for c in counts.values())


def test_different_categories_dont_cross_corroborate():
    """A financial claim and a leadership claim that share vocabulary still
    shouldn't corroborate each other."""
    items = [
        _vev("Revenue of $5B in fiscal 2023",
             "https://a.example/", category=EvidenceCategory.FINANCIAL),
        _vev("Revenue grew under his leadership in 2023",
             "https://b.example/", category=EvidenceCategory.LEADERSHIP),
    ]
    counts = compute_corroboration(items)
    assert all(c == 1 for c in counts.values())


def test_three_way_corroboration_groups_correctly():
    items = [
        _vev("Founded 1999 in Buenos Aires Argentina",
             "https://a.example/"),
        _vev("Founded 1999 Buenos Aires Argentina",
             "https://b.example/"),
        _vev("Founded 1999 in Argentina by Galperin",
             "https://c.example/"),
        _vev("Unrelated claim about products", "https://d.example/"),
    ]
    counts = compute_corroboration(items)
    # First 3 should be in same group of 3; last is alone
    assert counts[items[0].id] == 3
    assert counts[items[3].id] == 1


def test_stats_summary():
    items = [
        _vev("Founded 1999 Buenos Aires", "https://a.example/"),
        _vev("Founded 1999 Buenos Aires again", "https://b.example/"),
        _vev("Totally unrelated product launch in 2023",
             "https://c.example/"),
    ]
    s = corroboration_stats(items)
    assert s["total"] == 3
    assert s["corroborated"] == 2  # X and X-again form a group
    assert s["corroborated_share"] >= 0.6


def test_apply_corroboration_boost():
    # 3 signals, 1 corroborated → effective = 3 + 0.5 = 3.5
    assert apply_corroboration_to_recipe_signals(3, corroborated_signals=1) == 3.5
    # 2 signals, 2 corroborated → 2 + 1.0 = 3.0
    assert apply_corroboration_to_recipe_signals(2, corroborated_signals=2) == 3.0
    # 5 signals, 0 corroborated → 5.0
    assert apply_corroboration_to_recipe_signals(5, corroborated_signals=0) == 5.0


def test_unverified_items_ignored():
    items = [
        _vev("Claim X", "https://a.example/"),
        _vev("Claim X copy", "https://b.example/", status="unverifiable"),
    ]
    counts = compute_corroboration(items)
    # Unverified items are excluded from the map entirely
    assert items[0].id in counts
    assert items[1].id not in counts


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("MULTI_SOURCE_CORROBORATION", "0")
    import importlib
    from account_research.agents import corroboration as c
    importlib.reload(c)

    items = [
        _vev("Founded 1999 Buenos Aires", "https://a.example/"),
        _vev("Founded 1999 in Buenos Aires", "https://b.example/"),
    ]
    counts = c.compute_corroboration(items)
    assert all(v == 1 for v in counts.values())

    monkeypatch.setenv("MULTI_SOURCE_CORROBORATION", "1")
    importlib.reload(c)
