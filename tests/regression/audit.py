"""Compute accuracy / citation metrics over a saved BriefData JSON.

Used by both the pytest regression module and the standalone
`scripts/regression_run.py`. Pure-Python — no API calls, no DB.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from account_research.schemas.brief import BriefData


INSUFFICIENT_SENTINEL = "Insufficient public data"


@dataclass
class BriefMetrics:
    """Snapshot of accuracy-relevant counts for a single brief."""

    entity_name: str
    brief_id: str
    # Counts
    n_stats: int
    n_industries: int
    n_geographic_footprint: int
    n_sources: int
    n_timeline: int
    n_scorecard: int
    # Citation discipline
    pct_text_fields_cited: float
    pct_stats_cited: float
    pct_recap_stats_cited: float
    # Estimate discipline
    pct_badge_with_caveat: float  # 0 or 100 (single badge)
    n_methodology: int
    # Honest-empty discipline
    n_insufficient_sentinels: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_sentinel(text: str | None) -> bool:
    return bool(text) and INSUFFICIENT_SENTINEL.lower() in text.lower()


def audit_brief(brief: BriefData) -> BriefMetrics:
    """Walk a BriefData and compute the regression metrics."""
    # Text-bearing fields are cited iff they have non-empty evidence_ids
    # OR carry the "Insufficient public data" sentinel (honest empty).
    text_field_specs: list[tuple[str, list[Any]]] = [
        ("quick_take.body", [brief.quick_take.body, brief.quick_take.evidence_ids]),
    ]
    text_cited_total = 0
    text_cited_passed = 0
    text_cited_total += 1
    if (
        brief.quick_take.evidence_ids
        or _is_sentinel(brief.quick_take.body)
    ):
        text_cited_passed += 1

    for card in brief.who_they_are_cards:
        text_cited_total += 1
        if card.evidence_ids or _is_sentinel(card.body):
            text_cited_passed += 1
    for card in brief.dna_cards:
        text_cited_total += 1
        if card.evidence_ids or _is_sentinel(card.detail):
            text_cited_passed += 1
    for sig in brief.strategic_signals:
        text_cited_total += 1
        if sig.evidence_ids or _is_sentinel(sig.body):
            text_cited_passed += 1
    for row in brief.scorecard:
        text_cited_total += 1
        if row.evidence_ids or _is_sentinel(row.rationale):
            text_cited_passed += 1
    for sig in brief.key_signals:
        text_cited_total += 1
        if sig.evidence_ids or _is_sentinel(sig.body):
            text_cited_passed += 1
    for app in brief.recommended_approach:
        text_cited_total += 1
        if app.evidence_ids or _is_sentinel(app.body):
            text_cited_passed += 1
    for q in brief.discovery_questions:
        text_cited_total += 1
        if q.evidence_ids or _is_sentinel(q.question):
            text_cited_passed += 1
    for ns in brief.next_steps:
        text_cited_total += 1
        if ns.evidence_ids or _is_sentinel(ns.body):
            text_cited_passed += 1
    for m in brief.timeline:
        text_cited_total += 1
        if m.evidence_ids:
            text_cited_passed += 1

    pct_text = (
        100.0 * text_cited_passed / text_cited_total
        if text_cited_total else 100.0
    )

    # Stats: cited iff evidence_id is non-null OR value is the sentinel.
    def _stat_ok(value: str, eid: Any) -> bool:
        return eid is not None or _is_sentinel(value)

    n_stats_total = len(brief.stats)
    n_stats_cited = sum(1 for s in brief.stats if _stat_ok(s.value, s.evidence_id))
    pct_stats = 100.0 * n_stats_cited / n_stats_total if n_stats_total else 100.0

    n_recap_total = len(brief.recap_stats)
    n_recap_cited = sum(
        1 for s in brief.recap_stats if _stat_ok(s.value, s.evidence_id)
    )
    pct_recap = 100.0 * n_recap_cited / n_recap_total if n_recap_total else 100.0

    # Badge with caveat: when method_id is present, caveat must also be.
    badge = brief.hero.badge
    if badge is None or badge.method_id is None:
        pct_badge_with_caveat = 100.0  # vacuously satisfied — no estimate to caveat
    else:
        pct_badge_with_caveat = 100.0 if (badge.caveat or "").strip() else 0.0

    sentinels = 0
    if _is_sentinel(brief.quick_take.body):
        sentinels += 1
    sentinels += sum(1 for s in brief.stats if _is_sentinel(s.value))
    sentinels += sum(1 for s in brief.recap_stats if _is_sentinel(s.value))

    return BriefMetrics(
        entity_name=brief.hero.name,
        brief_id=str(brief.entity_id),
        n_stats=n_stats_total,
        n_industries=len(brief.industries),
        n_geographic_footprint=len(brief.geographic_footprint),
        n_sources=len(brief.sources),
        n_timeline=len(brief.timeline),
        n_scorecard=len(brief.scorecard),
        pct_text_fields_cited=round(pct_text, 1),
        pct_stats_cited=round(pct_stats, 1),
        pct_recap_stats_cited=round(pct_recap, 1),
        pct_badge_with_caveat=round(pct_badge_with_caveat, 1),
        n_methodology=len(brief.methodology),
        n_insufficient_sentinels=sentinels,
    )


def load_brief_from_disk(path: Path) -> BriefData:
    return BriefData.model_validate_json(path.read_text(encoding="utf-8"))


def evaluate(metrics: BriefMetrics, thresholds: dict[str, float]) -> list[str]:
    """Return a list of failure messages — empty when every threshold is met."""
    failures: list[str] = []
    for key, minimum in thresholds.items():
        actual = getattr(metrics, key, None)
        if actual is None:
            failures.append(f"unknown metric {key!r} in thresholds")
            continue
        if actual < minimum:
            failures.append(
                f"{key}: {actual} < {minimum} (entity={metrics.entity_name})"
            )
    return failures


def load_golden(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
