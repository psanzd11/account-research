"""Industry classifier — keyword heuristic that gates recipe selection."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from account_research.agents.industry_classifier import (
    classify_industry,
    industry_scores,
)
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)


def _vev(claim: str, raw_quote: str | None = None) -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=uuid4(),
        claim=claim,
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://example.com/p",
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote=raw_quote or claim,
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="verified", method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


def test_saas_heavy_ledger_classifies_saas():
    ledger = [
        _vev("Subscription SaaS platform"),
        _vev("ARR grew 30% YoY"),
        _vev("Per-seat pricing starts at $20"),
        _vev("Cohort analysis shows low churn"),
    ]
    assert classify_industry(ledger) == "saas"


def test_payments_heavy_ledger_classifies_payments():
    ledger = [
        _vev("Stripe is a payments processing company"),
        _vev("$1.4T in transaction volume last year"),
        _vev("Card network integrations across Visa and Mastercard"),
        _vev("Acquirer relationships in 50+ countries"),
    ]
    assert classify_industry(ledger) == "payments"


def test_consulting_heavy_ledger_classifies_consulting():
    ledger = [
        _vev("Project management consulting practice"),
        _vev("Delivered 50+ advisory engagements"),
        _vev("Professional services for client engagement"),
        _vev("Consultancy serves financial services clients"),
    ]
    assert classify_industry(ledger) == "consulting"


def test_ambiguous_classifies_other():
    # Mixed signals, no clear winner with required margin
    ledger = [
        _vev("Some SaaS-like features"),
        _vev("Also handles payments processing"),
    ]
    # 1 saas hit + 1 payments hit → below min_hits=2; should fall to "other"
    assert classify_industry(ledger) == "other"


def test_empty_ledger_returns_other():
    assert classify_industry([]) == "other"


def test_industry_scores_returns_counter():
    ledger = [
        _vev("SaaS company with MRR growth and per-seat pricing"),
    ]
    scores = industry_scores(ledger)
    assert scores["saas"] >= 2
    assert scores["payments"] == 0
