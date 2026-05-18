"""Methodology library: YAML parses; unregistered recipes raise."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from account_research.methodology import recipes as _recipes  # noqa: F401 — registers
from account_research.methodology.loader import (
    _count_fresh_signals,
    dispatch,
    is_registered,
    load_recipe,
)
from account_research.schemas.estimate import Estimate, SignalUsed
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)


def test_consulting_firm_yaml_parses():
    r = load_recipe("consulting_firm_revenue_v1")
    assert r.id == "consulting_firm_revenue_v1"
    assert r.applies_to == "company"
    assert r.minimum_required_signals >= 1
    assert any(s.signal == "years_in_operation" for s in r.required_signals)


def test_net_worth_yaml_parses():
    r = load_recipe("net_worth_individual_v1")
    assert r.id == "net_worth_individual_v1"
    assert r.applies_to == "person"
    assert "{signals_summary}" in r.caveat_template or r.caveat_template


def test_unknown_yaml_raises():
    with pytest.raises(FileNotFoundError):
        load_recipe("does_not_exist_v9")


def test_dispatch_to_unregistered_recipe_raises():
    """Dispatching to a method_id that no Python module registered must error,
    not silently fall back to inline computation (CLAUDE.md rule 4)."""
    assert not is_registered("phantom_recipe_v9")
    with pytest.raises(KeyError):
        dispatch("phantom_recipe_v9", entity_id=uuid4(), ledger=[])


def test_sprint2_recipes_are_registered():
    """After Sprint 2 all 3 recipes have Python implementations registered."""
    for mid in ("consulting_firm_revenue_v1", "net_worth_individual_v1", "saas_revenue_v1"):
        assert is_registered(mid), f"{mid} should be registered"


# ---------------------------------------------------------------------------
# A6 — Freshness window on recipes
# ---------------------------------------------------------------------------


class TestFreshnessField:
    """All 4 v1 recipes ship a minimum_evidence_freshness_days now."""

    def test_net_worth_has_freshness(self):
        assert load_recipe("net_worth_individual_v1").minimum_evidence_freshness_days == 365

    def test_consulting_firm_has_freshness(self):
        assert load_recipe("consulting_firm_revenue_v1").minimum_evidence_freshness_days == 365

    def test_saas_revenue_has_freshness(self):
        assert load_recipe("saas_revenue_v1").minimum_evidence_freshness_days == 365

    def test_public_disclosure_has_longer_freshness(self):
        """Public disclosures age slower than indirect signals."""
        assert load_recipe("public_disclosure_v1").minimum_evidence_freshness_days == 730


def _vev_at(claim: str, *, fetched_at: datetime, entity_id, **kw) -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=entity_id,
        claim=claim,
        category=kw.pop("category", EvidenceCategory.COMPANY_FACTS),
        source_url="https://example.com/p",
        source_type=kw.pop("source_type", SourceType.OFFICIAL_SITE),
        raw_quote=kw.pop("raw_quote", claim),
        fetched_at=fetched_at,
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="verified", method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


class TestCountFreshSignals:
    def test_returns_total_when_freshness_disabled(self):
        eid = uuid4()
        ev = _vev_at("a claim", fetched_at=datetime.now(timezone.utc) - timedelta(days=400),
                    entity_id=eid)
        sigs = [SignalUsed(evidence_id=ev.id, signal="x", value=1)]
        fresh, total = _count_fresh_signals(sigs, [ev], 0)
        assert fresh == 1 and total == 1

    def test_fresh_signal_counted(self):
        eid = uuid4()
        ev = _vev_at("a claim", fetched_at=datetime.now(timezone.utc) - timedelta(days=30),
                    entity_id=eid)
        sigs = [SignalUsed(evidence_id=ev.id, signal="x", value=1)]
        fresh, total = _count_fresh_signals(sigs, [ev], 365)
        assert fresh == 1 and total == 1

    def test_stale_signal_drops(self):
        eid = uuid4()
        ev = _vev_at("a claim", fetched_at=datetime.now(timezone.utc) - timedelta(days=400),
                    entity_id=eid)
        sigs = [SignalUsed(evidence_id=ev.id, signal="x", value=1)]
        fresh, total = _count_fresh_signals(sigs, [ev], 365)
        assert fresh == 0 and total == 1

    def test_signal_with_no_matching_ledger_entry_counts_stale(self):
        """Defensive: if a signal references an evidence_id absent from the
        ledger, count it as stale rather than crashing."""
        sigs = [SignalUsed(evidence_id=uuid4(), signal="ghost", value=1)]
        fresh, total = _count_fresh_signals(sigs, [], 365)
        assert fresh == 0 and total == 1


class TestFreshnessGateDegradesConfidence:
    """Integration: dispatch a real recipe with stale ledger evidence and
    verify the resulting Estimate has confidence=LOW + the stale caveat."""

    def _stale_consulting_ledger(self, entity_id):
        """Build a ledger that the consulting recipe will accept (≥3 signals)
        but whose evidence is all stale (>1 year old)."""
        stale = datetime.now(timezone.utc) - timedelta(days=400)
        return [
            _vev_at("Founded in 2020 in the Dominican Republic",
                    fetched_at=stale, entity_id=entity_id),
            _vev_at("50+ projects delivered across multiple industries",
                    fetched_at=stale, raw_quote="50 proyectos", entity_id=entity_id),
            _vev_at("Active in Dominican Republic, Panamá, Jamaica",
                    fetched_at=stale, raw_quote="República Dominicana, Panamá, Jamaica",
                    entity_id=entity_id,
                    category=EvidenceCategory.GEOGRAPHY),
            _vev_at("PMI Authorized Training Partner",
                    fetched_at=stale, raw_quote="PMI ATP partner",
                    entity_id=entity_id),
        ]

    def test_stale_ledger_degrades_to_low_with_caveat(self):
        eid = uuid4()
        ledger = self._stale_consulting_ledger(eid)
        result = dispatch(
            "consulting_firm_revenue_v1", entity_id=eid, ledger=ledger,
        )
        assert isinstance(result, Estimate)
        assert result.confidence == ConfidenceLevel.LOW
        assert "freshness window" in result.caveat_text

    def test_fresh_ledger_does_not_trigger_caveat(self):
        eid = uuid4()
        # Same shape but fetched_at is "now"
        ledger = [
            _vev_at("Founded in 2020 in the Dominican Republic",
                    fetched_at=datetime.now(timezone.utc), entity_id=eid),
            _vev_at("50+ projects delivered",
                    fetched_at=datetime.now(timezone.utc),
                    raw_quote="50 proyectos", entity_id=eid),
            _vev_at("Active in Dominican Republic, Panamá",
                    fetched_at=datetime.now(timezone.utc),
                    raw_quote="República Dominicana, Panamá",
                    entity_id=eid,
                    category=EvidenceCategory.GEOGRAPHY),
            _vev_at("PMI Authorized Training Partner",
                    fetched_at=datetime.now(timezone.utc),
                    raw_quote="PMI ATP partner", entity_id=eid),
        ]
        result = dispatch(
            "consulting_firm_revenue_v1", entity_id=eid, ledger=ledger,
        )
        assert isinstance(result, Estimate)
        # Fresh signals → no freshness caveat is appended (other caveats may be present)
        assert "freshness window" not in result.caveat_text
