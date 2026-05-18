"""Industry classifier — heuristic gate that keeps recipes from misfiring.

Why: the v1 Estimator routed every company through both consulting_firm and
saas recipes; signals were checked but industry was not. A payments giant
(Stripe) with disclosed headcount triggered saas_revenue_v1 and produced an
estimate two orders of magnitude below truth.

This module counts keyword occurrences in (claim + raw_quote) across the
ledger, picks the winning industry, and lets the Estimator decide which
recipes are even applicable.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Literal

from account_research.schemas.evidence import VerifiedEvidenceItem

Industry = Literal[
    "saas", "payments", "consulting", "fintech", "ecommerce",
    "marketplace", "ai", "hardware", "media", "other",
]

# Keyword sets. Match on word boundary to avoid false hits ("ai" inside
# "trained" etc). Multi-word phrases use `\b...\b` semantics.
_KEYWORDS: dict[Industry, list[str]] = {
    "saas": [
        r"\bSaaS\b", r"\bsubscription\b", r"\bper[- ]seat\b", r"\bARR\b",
        r"\bMRR\b", r"\bcohort\b", r"\bchurn\b", r"\bself[- ]serve\b",
        r"\bsoftware[- ]as[- ]a[- ]service\b",
    ],
    "payments": [
        r"\bpayment(?:s|\s+processing)\b", r"\btransaction volume\b",
        r"\bTPV\b", r"\bcard network\b", r"\bacquirer\b",
        r"\binterchange\b", r"\bmerchant\b", r"\bcheckout\b",
        r"\bpayment gateway\b", r"\bACH\b",
    ],
    "consulting": [
        r"\bconsulting\b", r"\bconsultancy\b", r"\badvisory\b",
        r"\bprojects delivered\b", r"\bclient engagement\b",
        r"\bproject management\b", r"\bprofessional services\b",
    ],
    "fintech": [
        r"\blending\b", r"\bneobank\b", r"\bwealth management\b",
        r"\bbrokerage\b", r"\bdigital bank\b", r"\binsurtech\b",
        r"\binvestment platform\b", r"\bregtech\b",
    ],
    "ecommerce": [
        r"\bonline retail\b", r"\bdirect[- ]to[- ]consumer\b", r"\bD2C\b",
        r"\bDTC\b", r"\bshipping\b", r"\bfulfillment\b",
        r"\border volume\b", r"\bGMV\b",
    ],
    "marketplace": [
        r"\bmarketplace\b", r"\btwo[- ]sided\b", r"\btake rate\b",
        r"\bbuyer[s]?\b.{0,30}\bseller[s]?\b",
        r"\bsupply[- ]side\b", r"\bdemand[- ]side\b",
    ],
    "ai": [
        r"\bmachine learning\b", r"\bgenerative AI\b",
        r"\bfoundation model\b", r"\bLLM\b", r"\binference\b",
        r"\bcomputer vision\b", r"\bNLP\b", r"\btraining run[s]?\b",
    ],
    "hardware": [
        r"\bsemiconductor\b", r"\bchip(?:set|\s+design)?\b",
        r"\bdevice manufactur\b", r"\bsupply chain\b.{0,30}\bcomponent\b",
        r"\bfab(?:rication)?\b", r"\bOEM\b",
    ],
    "media": [
        r"\bstreaming\b", r"\bcontent (?:catalog|library)\b",
        r"\bsubscriber base\b.{0,30}\b(?:show|series|film)\b",
        r"\bad[- ]supported\b", r"\bDAU\b", r"\bMAU\b",
    ],
}


_COMPILED: dict[Industry, list[re.Pattern]] = {
    ind: [re.compile(p, re.IGNORECASE) for p in patterns]
    for ind, patterns in _KEYWORDS.items()
}


def _ledger_text(ledger: Iterable[VerifiedEvidenceItem]) -> str:
    """Concatenate claim + raw_quote for keyword scanning."""
    return "\n".join(f"{ev.claim} {ev.raw_quote}" for ev in ledger)


def industry_scores(ledger: Iterable[VerifiedEvidenceItem]) -> Counter:
    """Return per-industry hit counts. Useful for debugging or ambiguity tests."""
    text = _ledger_text(ledger)
    scores: Counter = Counter()
    for ind, patterns in _COMPILED.items():
        for pat in patterns:
            scores[ind] += len(pat.findall(text))
    return scores


def classify_industry(
    ledger: Iterable[VerifiedEvidenceItem],
    *,
    min_hits: int = 2,
    margin: int = 1,
) -> Industry:
    """Pick the industry with the most keyword hits.

    Returns "other" when:
      - the winner has fewer than `min_hits` (signal too weak), or
      - the top two industries tie within `margin` (ambiguous).

    The defaults are deliberately strict — better to fall back to entity-type
    routing than to misclassify and pick a wrong-fit recipe.
    """
    scores = industry_scores(ledger)
    if not scores:
        return "other"
    ranked = scores.most_common()
    top_ind, top_score = ranked[0]
    if top_score < min_hits:
        return "other"
    if len(ranked) >= 2 and (top_score - ranked[1][1]) < margin:
        return "other"
    return top_ind  # type: ignore[return-value]
