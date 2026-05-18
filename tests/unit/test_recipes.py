"""Recipe tests: signal extraction + range computation for the 3 recipes."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from account_research.methodology import recipes as _recipes  # noqa: F401 — registers
from account_research.methodology.loader import dispatch
from account_research.schemas.estimate import Estimate, InsufficientSignals
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)


def _vev(claim: str, *, raw_quote: str | None = None,
         category: EvidenceCategory = EvidenceCategory.COMPANY_FACTS,
         source_type: SourceType = SourceType.OFFICIAL_SITE,
         entity_id=None) -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=entity_id or uuid4(),
        claim=claim,
        category=category,
        source_url="https://example.com/p",
        source_type=source_type,
        raw_quote=raw_quote or claim,
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="verified", method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


# ---------------------------------------------------------------------------
# consulting_firm_revenue_v1
# ---------------------------------------------------------------------------


class TestConsultingFirmRecipe:
    def test_bwpm_like_ledger_produces_estimate(self):
        entity_id = uuid4()
        ledger = [
            _vev("Founded in 2020 in the Dominican Republic", entity_id=entity_id),
            _vev("50+ projects delivered across multiple industries",
                 raw_quote="hemos gestionado con éxito más de 50 proyectos",
                 entity_id=entity_id),
            _vev("830 Followers, 71 Following, 293 Posts",
                 category=EvidenceCategory.OTHER, source_type=SourceType.SOCIAL,
                 raw_quote="830 Followers", entity_id=entity_id),
            _vev("Active in Dominican Republic, Panamá, Jamaica",
                 category=EvidenceCategory.GEOGRAPHY,
                 raw_quote="Servicios en República Dominicana, Panamá, Jamaica",
                 entity_id=entity_id),
            _vev("PMI Authorized Training Partner",
                 raw_quote="PMI ATP partner", entity_id=entity_id),
            _vev("Exclusive ClickUp Partner in Dominican Republic",
                 raw_quote="ClickUp partner exclusivo", entity_id=entity_id),
        ]
        result = dispatch("consulting_firm_revenue_v1", entity_id=entity_id, ledger=ledger)
        assert isinstance(result, Estimate), result
        assert result.method_id == "consulting_firm_revenue_v1"
        assert "$" in result.value_range
        assert "-" in result.value_range or "–" in result.value_range

    def test_empty_ledger_returns_insufficient(self):
        result = dispatch("consulting_firm_revenue_v1", entity_id=uuid4(), ledger=[])
        assert isinstance(result, InsufficientSignals)
        assert result.signals_present == 0


# ---------------------------------------------------------------------------
# net_worth_individual_v1
# ---------------------------------------------------------------------------


class TestNetWorthRecipe:
    def test_strong_signals_produces_estimate(self):
        entity_id = uuid4()
        ledger = [
            _vev("Founded Cemex Inc in 1994 as founder of construction tech",
                 raw_quote="founder of Cemex Inc, an international construction firm",
                 entity_id=entity_id),
            _vev("Founded Grupo MIA in 2009 as founder of base-of-pyramid housing",
                 raw_quote="founded Grupo MIA in 2009",
                 entity_id=entity_id),
            _vev("Founded Social Global Leaders in 2018",
                 raw_quote="created Social Global Leaders in 2018",
                 entity_id=entity_id),
            _vev("YPO member since 2015",
                 raw_quote="member of YPO chapter in Mexico City",
                 entity_id=entity_id),
        ]
        result = dispatch("net_worth_individual_v1", entity_id=entity_id, ledger=ledger)
        assert isinstance(result, Estimate), result
        assert result.method_id == "net_worth_individual_v1"

    def test_only_one_signal_insufficient(self):
        ledger = [_vev("Founded XCorp in 2020", raw_quote="founder of XCorp")]
        result = dispatch("net_worth_individual_v1", entity_id=uuid4(), ledger=ledger)
        assert isinstance(result, InsufficientSignals)


# ---------------------------------------------------------------------------
# saas_revenue_v1
# ---------------------------------------------------------------------------


class TestSaasRecipe:
    def test_headcount_drives_estimate(self):
        entity_id = uuid4()
        ledger = [
            _vev("Founded in 2015 by ex-Google engineers",
                 raw_quote="launched in 2015", entity_id=entity_id),
            _vev("250 employees across 4 offices",
                 raw_quote="250 employees worldwide", entity_id=entity_id),
            _vev("Serves 5,000 customers globally",
                 raw_quote="5,000 customers across the globe", entity_id=entity_id),
            _vev("Raised $80 million in Series B",
                 raw_quote="raised $80 million in a Series B round",
                 entity_id=entity_id),
        ]
        result = dispatch("saas_revenue_v1", entity_id=entity_id, ledger=ledger)
        assert isinstance(result, Estimate), result
        # 250 headcount × $120-220K = $30M-$55M
        assert "M" in result.value_range

    def test_insufficient_without_headcount_or_funding(self):
        ledger = [
            _vev("Founded in 2018",
                 raw_quote="launched in 2018", entity_id=uuid4()),
        ]
        result = dispatch("saas_revenue_v1", entity_id=uuid4(), ledger=ledger)
        assert isinstance(result, InsufficientSignals)

    def test_tier_guard_rejects_billion_scale_low_confidence(self):
        """Headcount × $120K with only 3 signals would estimate >$1B at 8000 heads.
        Tier-guard kicks in because confidence=LOW and high>$500M."""
        entity_id = uuid4()
        ledger = [
            _vev("Founded in 2010", raw_quote="founded in 2010", entity_id=entity_id),
            _vev("8000 employees globally",
                 raw_quote="8000 employees worldwide", entity_id=entity_id),
            _vev("Serves 5 million customers",
                 raw_quote="5,000,000 customers", entity_id=entity_id),
        ]
        result = dispatch("saas_revenue_v1", entity_id=entity_id, ledger=ledger)
        assert isinstance(result, InsufficientSignals), (
            "tier-guard should reject billion-scale estimate with only 3 signals + LOW confidence"
        )


# ---------------------------------------------------------------------------
# public_disclosure_v1
# ---------------------------------------------------------------------------


class TestPublicDisclosureRecipe:
    def test_matches_dollar_in_revenue_pattern(self):
        entity_id = uuid4()
        ledger = [
            _vev("Generated $5 billion in revenue last fiscal year",
                 raw_quote="$5 billion in revenue", entity_id=entity_id),
        ]
        result = dispatch("public_disclosure_v1", entity_id=entity_id, ledger=ledger)
        assert isinstance(result, Estimate)
        assert result.confidence.value == "high"
        assert "B" in result.value_range

    def test_reverse_pattern_revenue_of_dollars(self):
        entity_id = uuid4()
        ledger = [
            _vev("Annual revenue of $2.5 billion reported",
                 raw_quote="annual revenue of $2.5 billion",
                 entity_id=entity_id),
        ]
        result = dispatch("public_disclosure_v1", entity_id=entity_id, ledger=ledger)
        assert isinstance(result, Estimate)
        assert result.confidence.value == "high"

    def test_sec_filing_source_preferred(self):
        from account_research.schemas.evidence import SourceType
        entity_id = uuid4()
        ledger = [
            _vev("Press release mentions $3 billion in sales",
                 raw_quote="$3 billion in sales", entity_id=entity_id),
            _vev("10-K filing reports $4 billion in revenue",
                 raw_quote="$4 billion in revenue",
                 entity_id=entity_id, source_type=SourceType.SEC_FILING),
        ]
        result = dispatch("public_disclosure_v1", entity_id=entity_id, ledger=ledger)
        assert isinstance(result, Estimate)

    def test_no_match_returns_insufficient(self):
        ledger = [
            _vev("Founded in 2010 with 8000 employees",
                 raw_quote="founded 2010, 8000 employees globally",
                 entity_id=uuid4()),
            _vev("$1.4 trillion in transaction volume processed",
                 raw_quote="processed $1.4T in transaction volume",
                 entity_id=uuid4()),
        ]
        result = dispatch("public_disclosure_v1", entity_id=uuid4(), ledger=ledger)
        # No "revenue|sales|ARR" keyword anchored to a dollar → InsufficientSignals
        assert isinstance(result, InsufficientSignals)

    def test_ignores_transaction_volume(self):
        """Stripe's TPV ($1.4T processed) is NOT revenue — must not match."""
        ledger = [
            _vev("Processed $1.4 trillion in transaction volume",
                 raw_quote="$1.4 trillion in transaction volume",
                 entity_id=uuid4()),
        ]
        result = dispatch("public_disclosure_v1", entity_id=uuid4(), ledger=ledger)
        assert isinstance(result, InsufficientSignals)


# ---------------------------------------------------------------------------
# consulting_firm_revenue_v1 — tier guard
# ---------------------------------------------------------------------------


class TestConsultingTierGuard:
    def test_tier_guard_rejects_above_200M_low_confidence(self):
        """Consulting with 1500 heads → high ~$225M; with only 3 signals + LOW conf the guard fires."""
        entity_id = uuid4()
        ledger = [
            _vev("Founded in 2000", raw_quote="founded in 2000",
                 entity_id=entity_id),
            _vev("1500 employees worldwide",
                 raw_quote="1500 employees worldwide", entity_id=entity_id),
            _vev("Active globally in 30+ countries",
                 raw_quote="serves clients globally in 30 countries",
                 category=EvidenceCategory.GEOGRAPHY, entity_id=entity_id),
        ]
        result = dispatch("consulting_firm_revenue_v1", entity_id=entity_id, ledger=ledger)
        assert isinstance(result, InsufficientSignals), result
