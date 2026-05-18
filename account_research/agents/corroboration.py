"""Multi-source corroboration (Round 2 / G2).

When 2+ independent sources state the same factual claim, the assertion is
structurally more reliable than any single-source claim. v1 of the pipeline
ignored corroboration entirely — each evidence item was scored in isolation,
so a claim repeated across Bloomberg + Reuters + SEC counted as 3 separate
single-source items rather than 1 well-corroborated fact.

This module post-processes the verified ledger:

  1. Group verified items by claim-similarity (Jaccard ≥0.35 on the normalized
     claim text), de-deduplicating items that came from the SAME host.
  2. Items in a multi-source group get a virtual `corroboration_count` (the
     size of their group). Group size >=2 → considered "corroborated".
  3. Estimator recipes are given a `corroboration_score` per signal, which
     allows them to count a corroborated headcount item as 1.5× a single-
     source one for the purpose of `signals_present`.

Important: this does NOT modify the EvidenceItem schema. Corroboration data
is attached as a sibling dict keyed by evidence_id and consumed by recipes
via `apply_corroboration_to_ledger`.
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Iterable
from urllib.parse import urlparse
from uuid import UUID

from account_research.schemas.evidence import VerifiedEvidenceItem
from account_research.utils.text_normalize import normalize_for_match

# Feature flag (Round 2 / G2). Default ON.
_CORROBORATION_ENABLED = (
    os.environ.get("MULTI_SOURCE_CORROBORATION", "1").lower()
    not in ("0", "false", "no", "off")
)

# Threshold for grouping two claims together by token-Jaccard.
CLAIM_SIMILARITY_THRESHOLD = float(
    os.environ.get("CORROBORATION_JACCARD_THRESHOLD", "0.35")
)

# Stop words inline (same set as citation_validator, kept independent for
# debuggability).
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to",
    "for", "with", "as", "is", "are", "was", "were", "be", "been", "being",
    "by", "from", "this", "that", "these", "those", "it", "its", "their",
    "his", "her", "our", "your", "we", "they", "you", "he", "she",
    "has", "have", "had", "do", "does", "did", "not", "no",
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "y", "o", "pero", "en", "con", "por", "para", "como", "se", "su",
    "sus", "es", "son", "fue", "fueron", "ha", "han", "que",
})


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    normalized = normalize_for_match(text)
    out: set[str] = set()
    buf: list[str] = []
    for ch in normalized:
        if ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                t = "".join(buf)
                buf = []
                if len(t) >= 3 and t not in _STOPWORDS:
                    out.add(t)
    if buf:
        t = "".join(buf)
        if len(t) >= 3 and t not in _STOPWORDS:
            out.add(t)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split(":", 1)[0]
    except Exception:
        return ""


def compute_corroboration(
    items: Iterable[VerifiedEvidenceItem],
    *,
    threshold: float = CLAIM_SIMILARITY_THRESHOLD,
) -> dict[UUID, int]:
    """Compute the corroboration count for each verified item.

    Two items corroborate iff:
      - Both are verified
      - claim-text Jaccard ≥ threshold
      - They come from DIFFERENT hosts (same host = same source effectively)
      - They share at least one category context (avoids false positives
        between e.g. a financial claim and a leadership claim that happen to
        share vocabulary)

    Returns {evidence_id: count} where count is the size of the corroboration
    group the item belongs to. Singleton items get count=1.
    """
    verified = [
        i for i in items
        if getattr(i, "verification", None) is not None
        and i.verification.status == "verified"
    ]
    n = len(verified)
    if n == 0 or not _CORROBORATION_ENABLED:
        return {i.id: 1 for i in verified}

    token_sets = [_tokens(ev.claim) for ev in verified]
    hosts = [_host(str(ev.source_url)) for ev in verified]
    cats = [ev.category.value for ev in verified]

    # Union-find for grouping
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            if hosts[i] == hosts[j]:
                continue  # same host — not independent
            if cats[i] != cats[j]:
                continue  # cross-category false positives are too risky
            sim = _jaccard(token_sets[i], token_sets[j])
            if sim >= threshold:
                union(i, j)

    group_sizes: dict[int, int] = defaultdict(int)
    for i in range(n):
        group_sizes[find(i)] += 1

    return {
        verified[i].id: group_sizes[find(i)]
        for i in range(n)
    }


def corroboration_stats(
    items: list[VerifiedEvidenceItem],
) -> dict[str, float | int]:
    """Summary stats for logging / regression scoring."""
    counts = compute_corroboration(items)
    if not counts:
        return {"total": 0, "corroborated": 0, "corroborated_share": 0.0}
    corroborated = sum(1 for c in counts.values() if c >= 2)
    total = len(counts)
    return {
        "total": total,
        "corroborated": corroborated,
        "corroborated_share": round(corroborated / total, 3),
        "max_group_size": max(counts.values()),
    }


def apply_corroboration_to_recipe_signals(
    signals_present: int,
    *,
    corroborated_signals: int,
    boost_factor: float = 1.5,
) -> float:
    """Apply the 1.5× boost to recipe signal counting.

    Recipes call this to inflate `signals_present` based on how many of the
    contributing items were corroborated by multi-source evidence. A recipe
    that ordinarily needs 3 signals can now succeed with 2 single-source +
    1 corroborated (counted as 1.5×, total 3.5 ≥ 3).
    """
    if corroborated_signals == 0:
        return float(signals_present)
    boost = corroborated_signals * (boost_factor - 1.0)
    return float(signals_present) + boost
