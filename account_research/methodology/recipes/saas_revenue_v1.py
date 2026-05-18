"""Python implementation of the saas_revenue_v1 YAML recipe."""
from __future__ import annotations

import os
import re
import sys
from typing import Any

from account_research.methodology.loader import (
    RangeResult,
    Recipe,
    register,
)
from account_research.schemas.estimate import InsufficientSignals, SignalUsed
from account_research.schemas.evidence import ConfidenceLevel, VerifiedEvidenceItem

METHOD_ID = "saas_revenue_v1"

# Tier-guard threshold: a LOW-confidence indirect-signal estimate whose upper
# bound exceeds this is structurally wrong (companies at this scale almost
# always disclose revenue directly). Disable by setting to 0.
_TIER_GUARD_USD = float(os.environ.get("SAAS_TIER_GUARD_USD", "500000000"))

_HEADCOUNT_RE = re.compile(
    r"(\d{1,5})\+?\s*(?:employees|empleados|engineers|team members|people)",
    re.IGNORECASE,
)
_CUSTOMERS_RE = re.compile(
    r"(\d{1,7}(?:,\d{3})*)\+?\s*(?:customers|users|accounts|clientes|usuarios)",
    re.IGNORECASE,
)
_FUNDING_RE = re.compile(
    r"(?:raised|funding|Series\s+[A-Z])\s+\$?(\d{1,4})\s*(million|M|billion|B)",
    re.IGNORECASE,
)
_PRICING_RE = re.compile(
    r"\$(\d{1,4})\s*(?:per|/)\s*(?:user|month|seat)", re.IGNORECASE
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _now_year() -> int:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).year


def extract_signals(ledger: list[VerifiedEvidenceItem]) -> dict[str, Any]:
    signals: dict[str, Any] = {
        "years_active": None,
        "headcount_estimate": None,
        "customer_count_estimate": None,
        "pricing_tier_avg_usd": None,
        "funding_raised_usd": None,
        "_evidence": {},
    }

    pairs = [(ev.claim + " " + ev.raw_quote, ev) for ev in ledger]

    # years_active: earliest "founded/launched YYYY"
    for text, ev in pairs:
        if any(kw in text.lower() for kw in ("founded", "launched", "fundada")):
            for m in _YEAR_RE.finditer(text):
                y = int(m.group(0))
                if 1990 <= y <= _now_year():
                    signals["years_active"] = _now_year() - y
                    signals["_evidence"]["years_active"] = ev.id
                    break
            if signals["years_active"]:
                break

    # headcount
    for text, ev in pairs:
        m = _HEADCOUNT_RE.search(text)
        if m:
            n = int(m.group(1))
            if n >= (signals.get("headcount_estimate") or 0):
                signals["headcount_estimate"] = n
                signals["_evidence"]["headcount_estimate"] = ev.id

    # customers
    for text, ev in pairs:
        m = _CUSTOMERS_RE.search(text)
        if m:
            n = int(m.group(1).replace(",", ""))
            if n >= (signals.get("customer_count_estimate") or 0):
                signals["customer_count_estimate"] = n
                signals["_evidence"]["customer_count_estimate"] = ev.id

    # pricing
    prices: list[tuple[int, VerifiedEvidenceItem]] = []
    for text, ev in pairs:
        for m in _PRICING_RE.finditer(text):
            prices.append((int(m.group(1)), ev))
    if prices:
        avg = sum(p[0] for p in prices) // len(prices)
        signals["pricing_tier_avg_usd"] = avg
        signals["_evidence"]["pricing_tier_avg_usd"] = prices[0][1].id

    # funding
    for text, ev in pairs:
        m = _FUNDING_RE.search(text)
        if m:
            amount = int(m.group(1))
            unit = m.group(2).lower()
            usd = amount * 1_000_000 if unit.startswith("m") else amount * 1_000_000_000
            if usd > (signals.get("funding_raised_usd") or 0):
                signals["funding_raised_usd"] = usd
                signals["_evidence"]["funding_raised_usd"] = ev.id

    return signals


def _count_present(signals: dict[str, Any]) -> int:
    return sum(
        1 for k in (
            "years_active", "headcount_estimate", "customer_count_estimate",
            "pricing_tier_avg_usd", "funding_raised_usd",
        ) if signals.get(k)
    )


def compute(signals: dict[str, Any], recipe: Recipe) -> RangeResult | InsufficientSignals:
    present = _count_present(signals)
    if present < recipe.minimum_required_signals:
        return InsufficientSignals(
            metric=recipe.metric,
            method_id=METHOD_ID,
            signals_present=present,
            signals_required=recipe.minimum_required_signals,
            missing=[k for k in ("years_active", "headcount_estimate",
                                 "customer_count_estimate", "pricing_tier_avg_usd",
                                 "funding_raised_usd") if not signals.get(k)],
        )

    headcount = signals.get("headcount_estimate") or 0
    customers = signals.get("customer_count_estimate") or 0
    price = signals.get("pricing_tier_avg_usd") or 0
    funding = signals.get("funding_raised_usd") or 0

    if headcount >= 5:
        low = headcount * 120_000
        high = headcount * 220_000
        method = "headcount × revenue/FTE ($120-220K)"
    elif funding:
        low = funding * 0.30
        high = funding * 0.50
        method = "30-50% of disclosed funding"
    elif customers and price:
        low = customers * price * 12 * 0.6
        high = customers * price * 12 * 1.0
        method = "customers × ARPU × 12, discounted 0.6-1.0"
    else:
        return InsufficientSignals(
            metric=recipe.metric,
            method_id=METHOD_ID,
            signals_present=present,
            signals_required=recipe.minimum_required_signals,
            missing=["computable_combination"],
        )

    confidence = (
        ConfidenceLevel.MEDIUM
        if present >= 4 and headcount
        else ConfidenceLevel.LOW
    )

    # B2 tier-guard: a LOW-confidence estimate above $500M (env-overridable)
    # is structurally wrong — companies at this scale disclose revenue.
    if (
        _TIER_GUARD_USD > 0
        and confidence == ConfidenceLevel.LOW
        and present <= 3
        and high > _TIER_GUARD_USD
    ):
        return InsufficientSignals(
            metric=recipe.metric,
            method_id=METHOD_ID,
            signals_present=present,
            signals_required=recipe.minimum_required_signals + 1,
            missing=[
                "additional_signals_required_for_billion_scale_estimate",
                f"computed_high_${high/1e6:.0f}M_with_only_{present}_signals",
            ],
        )

    evidence_map: dict = signals.get("_evidence", {})
    signals_used = [
        SignalUsed(evidence_id=evidence_map[k], signal=k, value=signals[k])
        for k in ("years_active", "headcount_estimate", "customer_count_estimate",
                  "pricing_tier_avg_usd", "funding_raised_usd")
        if signals.get(k) and k in evidence_map
    ]

    return RangeResult(
        value_low=low,
        value_high=high,
        confidence=confidence,
        unit="USD/year",
        signals_used=signals_used,
        assumptions=[
            f"Method: {method}",
            "SaaS benchmark: $120-220K revenue per FTE (2024 surveys)",
            "Funding-to-ARR ratio: 30-50% (rule of thumb)",
            "Customer × ARPU discounted 0.6-1.0 for free tiers, churn, discounts",
        ],
        caveat_text=(
            f"Estimated via {method}. Privately held; not publicly disclosed."
        ),
    )


register(METHOD_ID, sys.modules[__name__])
