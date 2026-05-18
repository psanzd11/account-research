"""Public-disclosure recipe — short-circuit when a company's revenue has been
publicly disclosed.

Pattern matched (case-insensitive, on `claim + raw_quote`):
  - "$X.Y billion in revenue"
  - "$X million in sales"
  - "annual revenue of $Z billion"
  - "$N B ARR" / "$N M ARR"

Also fires when the evidence's `source_type == SEC_FILING` and a dollar figure
appears in the raw_quote with any of the revenue-class keywords nearby.
"""
from __future__ import annotations

import re
import sys
from typing import Any

from account_research.methodology.loader import (
    RangeResult,
    Recipe,
    register,
)
from account_research.schemas.estimate import InsufficientSignals, SignalUsed
from account_research.schemas.evidence import (
    ConfidenceLevel,
    SourceType,
    VerifiedEvidenceItem,
)

METHOD_ID = "public_disclosure_v1"

# Disqualifying tokens — if any of these appear in the matched window the
# dollar figure is NOT revenue. Common false positives: GMV, market cap,
# transaction volume (TPV), assets under management.
_DISQUALIFY_RE = re.compile(
    r"\b(GMV|gross merchandise|market\s*cap|valuation|TPV|"
    r"transaction\s*volume|payment\s*volume|AUM|assets\s+under|"
    r"total\s+assets|gross\s+volume|enterprise\s+value)\b",
    re.IGNORECASE,
)

# Tightened to 25 chars and explicit "revenue|sales|ARR" keyword set.
# "net revenue", "gross revenue" are subsumed by "revenue" already.
_REVENUE_RE = re.compile(
    r"\$\s*(\d{1,4}(?:\.\d+)?)\s*"
    r"(billion|million|B|M)\b"
    r"[\s\S]{0,25}?"
    r"\b(revenue|sales|ARR|annualized\s+revenue)\b",
    re.IGNORECASE,
)
# Same shape, but keyword precedes the amount: "annual revenue of $5 billion"
_REVENUE_RE_REVERSE = re.compile(
    r"\b(revenue|sales|ARR|annualized\s+revenue)\b"
    r"[\s\S]{0,25}?"
    r"\$\s*(\d{1,4}(?:\.\d+)?)\s*"
    r"(billion|million|B|M)\b",
    re.IGNORECASE,
)


def _is_real_revenue_match(text: str, match_start: int, match_end: int) -> bool:
    """Reject the match if a disqualifying token (GMV, market cap, TPV…) is
    inside the matched span. This is what was producing the $51.5B GMV match
    on MeLi when the actual revenue was $21B."""
    span = text[match_start:match_end]
    return _DISQUALIFY_RE.search(span) is None


def _to_usd(amount: float, unit: str) -> float:
    u = unit.lower()
    if u.startswith("b"):
        return amount * 1_000_000_000
    return amount * 1_000_000


def _extract_revenue(text: str) -> float | None:
    """Return the largest disclosed revenue figure in `text` (USD), or None.

    Skips matches whose span contains GMV / market-cap / TPV / valuation
    tokens — those are NOT revenue even though they sit next to the keyword.
    """
    best: float | None = None
    for m in _REVENUE_RE.finditer(text):
        if not _is_real_revenue_match(text, m.start(), m.end()):
            continue
        usd = _to_usd(float(m.group(1)), m.group(2))
        if best is None or usd > best:
            best = usd
    for m in _REVENUE_RE_REVERSE.finditer(text):
        if not _is_real_revenue_match(text, m.start(), m.end()):
            continue
        usd = _to_usd(float(m.group(2)), m.group(3))
        if best is None or usd > best:
            best = usd
    return best


def extract_signals(ledger: list[VerifiedEvidenceItem]) -> dict[str, Any]:
    signals: dict[str, Any] = {
        "disclosed_revenue_usd": None,
        "_evidence": {},
        "_disclosure_low": None,
        "_disclosure_high": None,
        "_source_was_sec_filing": False,
    }
    best_usd: float | None = None
    best_ev: VerifiedEvidenceItem | None = None
    sec_seen = False

    for ev in ledger:
        text = f"{ev.claim} {ev.raw_quote}"
        usd = _extract_revenue(text)
        if usd is None:
            continue
        is_sec = ev.source_type == SourceType.SEC_FILING
        if best_usd is None or usd > best_usd:
            best_usd = usd
            best_ev = ev
            sec_seen = sec_seen or is_sec
        elif is_sec and not sec_seen:
            # Prefer SEC disclosure as the anchor when present, even if smaller
            best_usd = usd
            best_ev = ev
            sec_seen = True

    if best_usd is not None and best_ev is not None:
        signals["disclosed_revenue_usd"] = best_usd
        signals["_evidence"]["disclosed_revenue_usd"] = best_ev.id
        signals["_source_was_sec_filing"] = sec_seen
        # Render a tight range around the disclosed figure (-5% / +10% to allow
        # for fiscal-year drift between disclosure and the brief). SEC filings
        # use ±5% on both sides.
        if sec_seen:
            signals["_disclosure_low"] = best_usd * 0.95
            signals["_disclosure_high"] = best_usd * 1.05
        else:
            signals["_disclosure_low"] = best_usd * 0.90
            signals["_disclosure_high"] = best_usd * 1.10

    return signals


def compute(signals: dict[str, Any], recipe: Recipe) -> RangeResult | InsufficientSignals:
    if not signals.get("disclosed_revenue_usd"):
        return InsufficientSignals(
            metric=recipe.metric,
            method_id=METHOD_ID,
            signals_present=0,
            signals_required=recipe.minimum_required_signals,
            missing=["disclosed_revenue_usd"],
        )

    disclosed = signals["disclosed_revenue_usd"]
    low = signals["_disclosure_low"]
    high = signals["_disclosure_high"]
    sec = bool(signals.get("_source_was_sec_filing"))
    evidence_map: dict = signals.get("_evidence", {})

    signals_used = [
        SignalUsed(
            evidence_id=evidence_map["disclosed_revenue_usd"],
            signal="disclosed_revenue_usd",
            value=disclosed,
        )
    ]

    source_label = "SEC filing" if sec else "company disclosure"
    caveat = (
        f"Based on publicly disclosed revenue of ~${disclosed/1e9:.1f}B "
        f"({source_label}). Range reflects fiscal-year drift."
        if disclosed >= 1_000_000_000
        else
        f"Based on publicly disclosed revenue of ~${disclosed/1e6:.0f}M "
        f"({source_label}). Range reflects fiscal-year drift."
    )

    return RangeResult(
        value_low=low,
        value_high=high,
        confidence=ConfidenceLevel.HIGH,
        unit="USD/year",
        signals_used=signals_used,
        assumptions=[
            f"Anchor: disclosed revenue figure (${disclosed:,.0f})",
            f"Source class: {source_label}",
            "Range = anchor ±5% (SEC) / ±10% (other) for fiscal-year drift",
        ],
        caveat_text=caveat,
    )


register(METHOD_ID, sys.modules[__name__])
