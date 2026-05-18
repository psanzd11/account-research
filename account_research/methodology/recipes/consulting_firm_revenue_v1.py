"""Python implementation of the consulting_firm_revenue_v1 YAML recipe.

Signal extraction is regex/heuristic over claim text — no LLM call (SPEC §4.3).
Computation mirrors the formula in the YAML. See [[recipes-as-python-modules]].
"""
from __future__ import annotations

import os
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

METHOD_ID = "consulting_firm_revenue_v1"

# Tier-guard threshold: consulting firms above $200M revenue are rare in
# LATAM; a LOW-confidence indirect estimate above this is suspect. Disable
# with 0.
_TIER_GUARD_USD = float(os.environ.get("CONSULTING_TIER_GUARD_USD", "200000000"))


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_PROJECT_NUM_RE = re.compile(
    r"(\d{1,5})\+?\s*(?:projects|proyectos)", re.IGNORECASE
)
_HEADCOUNT_RE = re.compile(
    r"(\d{1,4})\+?\s*(?:employees|empleados|team members|colaboradores|professionals)",
    re.IGNORECASE,
)
_FOLLOWERS_RE = re.compile(r"(\d{1,4}(?:[,.]\d{3})*)\s*(?:Followers|followers|seguidores)")
_PARTNER_RE = re.compile(
    r"(?:partner|authorized|ATP|certified|partnership)", re.IGNORECASE
)


def _now_year() -> int:
    return datetime.now(timezone.utc).year


def _country_mentions(ledger: list[VerifiedEvidenceItem]) -> set[str]:
    """Return distinct country names found in geography-category claims."""
    countries = set()
    keywords = {
        "dominican republic": "Dominican Republic",
        "republica dominicana": "Dominican Republic",
        "república dominicana": "Dominican Republic",
        "panama": "Panama",
        "panamá": "Panama",
        "jamaica": "Jamaica",
        "mexico": "Mexico",
        "méxico": "Mexico",
        "colombia": "Colombia",
        "ecuador": "Ecuador",
        "guatemala": "Guatemala",
        "united states": "USA",
        "estados unidos": "USA",
        "usa": "USA",
        "latin america": "LATAM",
        "latinoamérica": "LATAM",
        "latinoamerica": "LATAM",
        "caribbean": "Caribbean",
    }
    for ev in ledger:
        text = f"{ev.claim} {ev.raw_quote}".lower()
        for key, name in keywords.items():
            if key in text:
                countries.add(name)
    return countries


def _geographic_scope(countries: set[str]) -> str | None:
    if not countries:
        return None
    if "LATAM" in countries or len(countries) >= 4:
        return "global" if "USA" in countries and "LATAM" in countries else "multi_country"
    if len(countries) == 1:
        return "single_country"
    return "multi_country"


def extract_signals(ledger: list[VerifiedEvidenceItem]) -> dict[str, Any]:
    signals: dict[str, Any] = {
        "years_in_operation": None,
        "project_count": None,
        "headcount_estimate": None,
        "geographic_scope": None,
        "strategic_partnerships": None,
        "_evidence": {},  # method_id -> evidence_id for traceability
    }

    # years_in_operation: oldest 19xx/20xx year in claims about founding
    founding_years: list[tuple[int, VerifiedEvidenceItem]] = []
    for ev in ledger:
        text = (ev.claim + " " + ev.raw_quote).lower()
        if any(kw in text for kw in ("founded", "founding", "fundada", "establecida", "inicios", "launched")):
            for m in _YEAR_RE.finditer(text):
                year = int(m.group(0))
                if 1980 <= year <= _now_year():
                    founding_years.append((year, ev))
                    break
    if founding_years:
        year, ev = min(founding_years, key=lambda t: t[0])
        signals["years_in_operation"] = _now_year() - year
        signals["_evidence"]["years_in_operation"] = ev.id

    # project_count: take the highest number found near "projects"/"proyectos"
    project_candidates: list[tuple[int, VerifiedEvidenceItem]] = []
    for ev in ledger:
        for m in _PROJECT_NUM_RE.finditer(ev.claim + " " + ev.raw_quote):
            project_candidates.append((int(m.group(1)), ev))
    if project_candidates:
        n, ev = max(project_candidates, key=lambda t: t[0])
        signals["project_count"] = n
        signals["_evidence"]["project_count"] = ev.id

    # headcount_estimate: explicit if found; else followers/50 as proxy
    explicit_head: list[tuple[int, VerifiedEvidenceItem]] = []
    follower_head: list[tuple[int, VerifiedEvidenceItem]] = []
    for ev in ledger:
        text = ev.claim + " " + ev.raw_quote
        for m in _HEADCOUNT_RE.finditer(text):
            explicit_head.append((int(m.group(1)), ev))
        for m in _FOLLOWERS_RE.finditer(text):
            raw = m.group(1).replace(",", "").replace(".", "")
            try:
                follower_head.append((int(raw) // 50, ev))
            except ValueError:
                pass
    if explicit_head:
        n, ev = max(explicit_head, key=lambda t: t[0])
        signals["headcount_estimate"] = n
        signals["_evidence"]["headcount_estimate"] = ev.id
    elif follower_head:
        n, ev = max(follower_head, key=lambda t: t[0])
        signals["headcount_estimate"] = max(n, 1)  # tiny consultancies are at least 1
        signals["_evidence"]["headcount_estimate"] = ev.id

    # geographic_scope
    countries = _country_mentions(ledger)
    scope = _geographic_scope(countries)
    if scope:
        signals["geographic_scope"] = scope
        # pick any geography-category evidence item as reference
        for ev in ledger:
            if ev.category.value == "geography":
                signals["_evidence"]["geographic_scope"] = ev.id
                break

    # strategic_partnerships: count distinct partner mentions in recognition/products
    partner_evs: list[VerifiedEvidenceItem] = []
    seen_keywords: set[str] = set()
    for ev in ledger:
        text = ev.claim + " " + ev.raw_quote
        if _PARTNER_RE.search(text):
            # de-dupe by first 30 chars of the claim
            key = ev.claim[:30].lower()
            if key not in seen_keywords:
                seen_keywords.add(key)
                partner_evs.append(ev)
    if partner_evs:
        signals["strategic_partnerships"] = len(partner_evs)
        signals["_evidence"]["strategic_partnerships"] = partner_evs[0].id

    return signals


def compute(signals: dict[str, Any], recipe: Recipe) -> RangeResult | InsufficientSignals:
    years = signals.get("years_in_operation")
    projects = signals.get("project_count")
    headcount = signals.get("headcount_estimate")
    scope = signals.get("geographic_scope")
    partnerships = signals.get("strategic_partnerships") or 0
    evidence_map: dict = signals.get("_evidence", {})

    present = sum(1 for k in ("years_in_operation", "project_count", "headcount_estimate",
                              "geographic_scope", "strategic_partnerships")
                  if signals.get(k))
    if present < recipe.minimum_required_signals:
        return InsufficientSignals(
            metric=recipe.metric,
            method_id=METHOD_ID,
            signals_present=present,
            signals_required=recipe.minimum_required_signals,
            missing=[k for k in ("years_in_operation", "project_count",
                                 "headcount_estimate", "geographic_scope",
                                 "strategic_partnerships") if not signals.get(k)],
        )

    # Defaults when a signal is missing but minimum is met
    years = years or 5
    projects = projects or 0
    headcount = headcount or 0

    effective_projects = projects * 0.7
    annual_from_projects_low = effective_projects * 30_000 / max(years, 1) if projects else 0
    annual_from_projects_high = effective_projects * 60_000 / max(years, 1) if projects else 0
    annual_from_headcount_low = headcount * 80_000 if headcount else 0
    annual_from_headcount_high = headcount * 150_000 if headcount else 0

    # If both cross-checks present, average; else use whichever is present
    if projects and headcount:
        annual_low = (annual_from_projects_low + annual_from_headcount_low) / 2
        annual_high = (annual_from_projects_high + annual_from_headcount_high) / 2
    elif projects:
        annual_low = annual_from_projects_low
        annual_high = annual_from_projects_high
    elif headcount:
        annual_low = annual_from_headcount_low
        annual_high = annual_from_headcount_high
    else:
        return InsufficientSignals(
            metric=recipe.metric,
            method_id=METHOD_ID,
            signals_present=present,
            signals_required=recipe.minimum_required_signals,
            missing=["project_count_or_headcount"],
        )

    geo_mult = {"single_city": 1.0, "single_country": 1.2,
                "multi_country": 1.5, "global": 2.0}.get(scope or "single_country", 1.0)
    annual_low *= geo_mult
    annual_high *= geo_mult

    partner_mult = 1.1 ** partnerships
    annual_low *= partner_mult
    annual_high *= partner_mult

    if present >= 4 and (headcount or 0) >= 5:
        confidence = ConfidenceLevel.MEDIUM
    else:
        confidence = ConfidenceLevel.LOW

    # B2 tier-guard: LOW-confidence consulting estimate above $200M is suspect.
    if (
        _TIER_GUARD_USD > 0
        and confidence == ConfidenceLevel.LOW
        and present <= 3
        and annual_high > _TIER_GUARD_USD
    ):
        return InsufficientSignals(
            metric=recipe.metric,
            method_id=METHOD_ID,
            signals_present=present,
            signals_required=recipe.minimum_required_signals + 1,
            missing=[
                "additional_signals_required_for_large_estimate",
                f"computed_high_${annual_high/1e6:.0f}M_with_only_{present}_signals",
            ],
        )

    signals_used = [
        SignalUsed(
            evidence_id=evidence_map[k],
            signal=k,
            value=signals[k],
        )
        for k in ("years_in_operation", "project_count", "headcount_estimate",
                  "geographic_scope", "strategic_partnerships")
        if signals.get(k) and k in evidence_map
    ]

    caveat = (
        f"Estimated from {years} yrs operation, {projects} projects "
        f"(discounted 30% for marketing), ~{headcount} headcount, "
        f"and {scope or 'unknown'} reach. Privately held; not publicly disclosed."
    )
    assumptions = [
        "Project count discounted by 30% for marketing inflation",
        "Avg project value $30-60K based on LATAM PM consultancy benchmarks (2024-2025)",
        "Revenue per FTE $80-150K typical for consulting",
        f"Each named strategic partnership adds 10% (×{partner_mult:.2f} total)",
        f"Geographic multiplier: {scope}={geo_mult}",
    ]

    return RangeResult(
        value_low=annual_low,
        value_high=annual_high,
        confidence=confidence,
        unit="USD/year",
        signals_used=signals_used,
        assumptions=assumptions,
        caveat_text=caveat,
    )


register(METHOD_ID, sys.modules[__name__])
