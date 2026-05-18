"""Sprint 3.3 — Researcher batch dedup.

After the LLM emits an EvidenceBatch, items sharing
`(normalize(claim), source_url)` are collapsed into one, keeping the
highest-confidence variant. This prevents Researcher from citing the same
Wikipedia paragraph multiple times when answering across categories.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from account_research.agents.researcher import dedup_evidence
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceItem,
    SourceType,
)


def _ev(
    claim: str,
    url: str = "https://x.test/a",
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM,
    category: EvidenceCategory = EvidenceCategory.COMPANY_FACTS,
) -> EvidenceItem:
    return EvidenceItem(
        entity_id=uuid4(),
        claim=claim,
        category=category,
        source_url=url,
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote=claim,
        fetched_at=datetime.now(timezone.utc),
        confidence=confidence,
    )


def test_returns_input_unchanged_when_no_duplicates():
    items = [
        _ev("Founded in 2020", url="https://a.com/"),
        _ev("Has 250 employees", url="https://b.com/"),
    ]
    out = dedup_evidence(items)
    assert len(out) == 2


def test_collapses_duplicates_keeping_highest_confidence():
    """Same claim + same URL twice — keep the verified-confidence variant."""
    low = _ev("Founded in 2020", url="https://x.com/about", confidence=ConfidenceLevel.MEDIUM)
    high = _ev("Founded in 2020", url="https://x.com/about", confidence=ConfidenceLevel.HIGH)
    out = dedup_evidence([low, high])
    assert len(out) == 1
    assert out[0].confidence == ConfidenceLevel.HIGH


def test_normalizes_claim_whitespace_and_case():
    """Same claim with different whitespace / case is still a duplicate."""
    a = _ev("Founded in 2020", url="https://x.com/p", confidence=ConfidenceLevel.MEDIUM)
    b = _ev("  founded in 2020  ", url="https://x.com/p", confidence=ConfidenceLevel.HIGH)
    out = dedup_evidence([a, b])
    assert len(out) == 1
    assert out[0].confidence == ConfidenceLevel.HIGH


def test_different_url_same_claim_kept():
    """Same claim from two different sources is corroboration, not dedup."""
    a = _ev("Founded in 2020", url="https://a.com/", confidence=ConfidenceLevel.HIGH)
    b = _ev("Founded in 2020", url="https://b.com/", confidence=ConfidenceLevel.HIGH)
    out = dedup_evidence([a, b])
    assert len(out) == 2


def test_different_claim_same_url_kept():
    """Same URL, different claims about it — keep both."""
    a = _ev("Founded in 2020", url="https://x.com/about")
    b = _ev("Has 250 employees", url="https://x.com/about")
    out = dedup_evidence([a, b])
    assert len(out) == 2


def test_preserves_first_item_id_when_collapsing():
    """The ID of the kept item should be the higher-confidence one's ID
    so downstream references don't dangle."""
    low = _ev("Same fact", url="https://x.com/p", confidence=ConfidenceLevel.MEDIUM)
    high = _ev("Same fact", url="https://x.com/p", confidence=ConfidenceLevel.HIGH)
    out = dedup_evidence([low, high])
    assert len(out) == 1
    assert out[0].id == high.id
