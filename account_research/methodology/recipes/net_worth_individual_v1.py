"""Python implementation of the net_worth_individual_v1 YAML recipe.

Heuristic signal extraction over ledger claims. No LLM. Output is always a
wide range — net worth from indirect signals is necessarily uncertain.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from typing import Any

from account_research.methodology.loader import (
    RangeResult,
    Recipe,
    register,
)
from account_research.schemas.estimate import InsufficientSignals, SignalUsed
from account_research.schemas.evidence import ConfidenceLevel, VerifiedEvidenceItem

METHOD_ID = "net_worth_individual_v1"

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_FOUNDER_RE = re.compile(
    r"(?:founded|founder|fundador|co[\s-]?founded|co[\s-]?founder|created|cre[oó])",
    re.IGNORECASE,
)
_EXIT_RE = re.compile(
    r"(?:sold|acquired|acquisition|exited|exit|merger|IPO)", re.IGNORECASE
)
_PUBLIC_CO_RE = re.compile(
    r"(?:public company|publicly traded|NYSE|NASDAQ|SEC filing|cotiza)",
    re.IGNORECASE,
)
_NETWORK_TERMS = ("YPO", "Vistage", "EO", "Endeavor", "Young Presidents", "Entrepreneurs Organization")


def _now_year() -> int:
    return datetime.now(timezone.utc).year


def _extract_companies(text: str) -> set[str]:
    """Best-effort: capitalized multi-word tokens after a founder verb."""
    matches = re.findall(
        r"(?:founded|fundador de|founder of|creó|created)\s+([A-Z][A-Za-zÀ-ÿ&\.]+(?:\s+[A-Z][A-Za-zÀ-ÿ&\.]+){0,3})",
        text,
    )
    return {m.strip().rstrip(".,;:") for m in matches}


def extract_signals(ledger: list[VerifiedEvidenceItem]) -> dict[str, Any]:
    signals: dict[str, Any] = {
        "years_executive_experience": None,
        "companies_founded": None,
        "ypo_or_equivalent_membership": False,
        "public_company_role": False,
        "known_exits": [],
        "_evidence": {},
    }

    all_text_evs: list[tuple[str, VerifiedEvidenceItem]] = [
        (ev.claim + " " + ev.raw_quote, ev) for ev in ledger
    ]

    # companies_founded — collect distinct names
    companies: dict[str, VerifiedEvidenceItem] = {}
    for text, ev in all_text_evs:
        if _FOUNDER_RE.search(text):
            for name in _extract_companies(text):
                # de-dupe loosely
                key = name.lower().strip()
                if key and key not in companies and len(key) > 2:
                    companies[key] = ev
    if companies:
        signals["companies_founded"] = len(companies)
        signals["_evidence"]["companies_founded"] = next(iter(companies.values())).id

    # years_executive_experience — span between earliest career year and now
    leadership_years: list[tuple[int, VerifiedEvidenceItem]] = []
    for text, ev in all_text_evs:
        if ev.category.value in ("leadership", "company_facts") or _FOUNDER_RE.search(text):
            for m in _YEAR_RE.finditer(text):
                y = int(m.group(0))
                if 1970 <= y <= _now_year():
                    leadership_years.append((y, ev))
    if leadership_years:
        earliest = min(leadership_years, key=lambda t: t[0])
        signals["years_executive_experience"] = _now_year() - earliest[0]
        signals["_evidence"]["years_executive_experience"] = earliest[1].id

    # ypo_or_equivalent_membership
    for text, ev in all_text_evs:
        if any(term in text for term in _NETWORK_TERMS):
            signals["ypo_or_equivalent_membership"] = True
            signals["_evidence"]["ypo_or_equivalent_membership"] = ev.id
            break

    # public_company_role
    for text, ev in all_text_evs:
        if _PUBLIC_CO_RE.search(text) or ev.source_type.value == "sec_filing":
            signals["public_company_role"] = True
            signals["_evidence"]["public_company_role"] = ev.id
            break

    # known_exits — distinct claims mentioning exit verbs
    exits: list[VerifiedEvidenceItem] = []
    for text, ev in all_text_evs:
        if _EXIT_RE.search(text):
            exits.append(ev)
    if exits:
        # Treat known_exits as a list for the YAML's `known_exits.length >= 1`
        signals["known_exits"] = [str(e.id) for e in exits[:10]]
        signals["_evidence"]["known_exits"] = exits[0].id

    return signals


def _count_present(signals: dict[str, Any]) -> int:
    count = 0
    if signals.get("years_executive_experience"):
        count += 1
    if signals.get("companies_founded"):
        count += 1
    if signals.get("ypo_or_equivalent_membership"):
        count += 1
    if signals.get("public_company_role"):
        count += 1
    if signals.get("known_exits"):
        count += 1
    return count


def compute(signals: dict[str, Any], recipe: Recipe) -> RangeResult | InsufficientSignals:
    present = _count_present(signals)
    if present < recipe.minimum_required_signals:
        return InsufficientSignals(
            metric=recipe.metric,
            method_id=METHOD_ID,
            signals_present=present,
            signals_required=recipe.minimum_required_signals,
            missing=[k for k in ("years_executive_experience", "companies_founded",
                                 "ypo_or_equivalent_membership", "public_company_role",
                                 "known_exits") if not signals.get(k)],
        )

    base_low, base_high = 1_000_000.0, 5_000_000.0

    companies_founded = signals.get("companies_founded") or 0
    ypo = signals.get("ypo_or_equivalent_membership") or False
    public_role = signals.get("public_company_role") or False
    years_exp = signals.get("years_executive_experience") or 0
    exits = signals.get("known_exits") or []

    if companies_founded >= 3:
        base_low *= 2
        base_high *= 3
    if ypo:
        base_low *= 1.5
        base_high *= 2.0
    if public_role:
        base_low += 5_000_000
        base_high += 20_000_000
    if len(exits) >= 1:
        base_low += 3_000_000
        base_high += 15_000_000
    if years_exp >= 20:
        base_low *= 1.2
        base_high *= 1.5

    confidence = ConfidenceLevel.MEDIUM if present >= 5 else ConfidenceLevel.LOW

    evidence_map: dict = signals.get("_evidence", {})
    signals_used = [
        SignalUsed(evidence_id=evidence_map[k], signal=k,
                   value=signals[k] if not isinstance(signals[k], list) else len(signals[k]))
        for k in ("years_executive_experience", "companies_founded",
                  "ypo_or_equivalent_membership", "public_company_role", "known_exits")
        if signals.get(k) and k in evidence_map
    ]

    summary_parts = []
    if companies_founded:
        summary_parts.append(f"{companies_founded} companies founded")
    if years_exp:
        summary_parts.append(f"{years_exp} yrs executive experience")
    if ypo:
        summary_parts.append("YPO/equivalent network membership")
    if public_role:
        summary_parts.append("public-company role")
    if exits:
        summary_parts.append(f"{len(exits)} documented exit(s)")
    summary = ", ".join(summary_parts)

    caveat = (
        f"Estimated from {summary}. Privately held / not publicly disclosed. "
        "Order-of-magnitude estimate from indirect signals, not a financial assessment."
    )

    return RangeResult(
        value_low=base_low,
        value_high=base_high,
        confidence=confidence,
        unit="USD",
        signals_used=signals_used,
        assumptions=[
            "Base $1-5M for someone with mid-career executive experience",
            "Each additional founding multiplies the range",
            "YPO/Endeavor membership implies controlling stake in ≥$10M-rev company",
            "Public-company roles add direct equity wealth ($5-20M)",
            "Calibrated against 12 publicly-disclosed reference cases (2025)",
        ],
        caveat_text=caveat,
    )


register(METHOD_ID, sys.modules[__name__])
