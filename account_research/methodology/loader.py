"""Methodology library: YAML metadata + Python computation modules.

Each estimation recipe is split across two files:

  methodology/<method_id>.yaml              ← metadata (required_signals,
                                              weights, minimum_required_signals,
                                              confidence_rule, caveat_template)
  account_research/methodology/recipes/<method_id>.py  ← Python computation

The Python module registers itself via `register(METHOD_ID, RecipeImpl(...))`.
The Estimator agent calls `dispatch(method_id, ledger)` which:
  1) loads the YAML metadata,
  2) asks the recipe to extract signals from the ledger,
  3) checks against minimum_required_signals,
  4) calls the recipe's compute() and returns Estimate | InsufficientSignals.

This keeps formulas debuggable Python (no eval) while leaving YAML readable as
maintainer-facing spec. See [[recipes-as-python-modules]] in MEMORY.md.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field

from account_research.schemas.estimate import (
    Estimate,
    InsufficientSignals,
    SignalUsed,
)
from account_research.schemas.evidence import (
    ConfidenceLevel,
    VerifiedEvidenceItem,
)

logger = logging.getLogger("methodology")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_YAML_DIR = REPO_ROOT / "methodology"


def _yaml_dir() -> Path:
    return Path(os.environ.get("METHODOLOGY_DIR", DEFAULT_YAML_DIR))


# ---------------------------------------------------------------------------
# Recipe YAML metadata model
# ---------------------------------------------------------------------------


class RequiredSignal(BaseModel):
    model_config = ConfigDict(extra="allow")

    signal: str
    type: str
    weight: float | None = None
    description: str | None = None
    source: str | None = None
    notes: str | None = None


class ConfidenceRule(BaseModel):
    """One branch of `confidence_rule:` from the YAML."""

    model_config = ConfigDict(extra="allow")

    if_: str | None = Field(default=None, alias="if")
    else_: bool = Field(default=False, alias="else")
    confidence: ConfidenceLevel | None = None


class Recipe(BaseModel):
    """Parsed YAML metadata. Computation lives in the matching Python module."""

    model_config = ConfigDict(extra="allow")

    id: str
    applies_to: str
    metric: str
    unit: str | None = None
    output_type: str = "range"
    description: str | None = None
    required_signals: list[RequiredSignal] = Field(default_factory=list)
    minimum_required_signals: int = 1
    # A6 — Freshness window. Signals derived from evidence older than this
    # many days are still used in compute() but do NOT count toward the
    # post-compute freshness gate. When the count of fresh signals drops
    # below `minimum_required_signals`, the dispatcher degrades the result
    # confidence to LOW and appends a stale-evidence caveat. 0 disables.
    minimum_evidence_freshness_days: int = Field(default=0, ge=0)
    caveat_template: str = ""
    notes: str | None = None


def load_recipe(method_id: str) -> Recipe:
    """Load and validate the YAML for `method_id`."""
    path = _yaml_dir() / f"{method_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"recipe YAML not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Recipe.model_validate(data)


# ---------------------------------------------------------------------------
# Recipe Python interface + registry
# ---------------------------------------------------------------------------


@dataclass
class RangeResult:
    """What a recipe's compute() returns when signals are sufficient."""

    value_low: float
    value_high: float
    confidence: ConfidenceLevel
    unit: str
    signals_used: list[SignalUsed]
    assumptions: list[str]
    caveat_text: str

    def format_value_range(self) -> str:
        """Render the value as a brief-friendly range string."""
        return _format_money_range(self.value_low, self.value_high, self.unit)


class RecipeImpl(Protocol):
    """Contract every recipe Python module exposes."""

    METHOD_ID: str

    def extract_signals(self, ledger: list[VerifiedEvidenceItem]) -> dict[str, Any]: ...

    def compute(
        self, signals: dict[str, Any], recipe: Recipe
    ) -> RangeResult | InsufficientSignals: ...


_REGISTRY: dict[str, RecipeImpl] = {}


def register(method_id: str, impl: RecipeImpl) -> None:
    """Recipe modules call this at import time."""
    _REGISTRY[method_id] = impl


def is_registered(method_id: str) -> bool:
    return method_id in _REGISTRY


def dispatch(
    method_id: str,
    *,
    entity_id: UUID,
    ledger: list[VerifiedEvidenceItem],
    corroboration_map: dict[UUID, int] | None = None,
) -> Estimate | InsufficientSignals:
    """Run a recipe end-to-end: load YAML, extract signals, compute, wrap.

    `corroboration_map` (Round 2 / G2): {evidence_id: group_size}. When a
    signal's underlying evidence_id has group_size ≥ 2 (multi-source), the
    signal counts 1.5× toward `signals_present` — allowing borderline LOW
    estimates to upgrade to MEDIUM.
    """
    if method_id not in _REGISTRY:
        raise KeyError(
            f"recipe {method_id!r} not registered. "
            f"Import account_research.methodology.recipes.{method_id} to register it."
        )
    recipe = load_recipe(method_id)
    impl = _REGISTRY[method_id]

    signals = impl.extract_signals(ledger)
    # Stash corroboration data so recipes can use it in compute()
    if corroboration_map is not None:
        signals["_corroboration_map"] = corroboration_map

    # Keys starting with "_" are recipe-internal bookkeeping (e.g. evidence
    # mappings), not signals — don't count them.
    public_signals = {k: v for k, v in signals.items() if not k.startswith("_")}
    present_raw = sum(1 for v in public_signals.values() if v not in (None, [], "", False))

    # G2 corroboration boost: each signal whose underlying evidence_id is
    # multi-source counted in `corroboration_map` adds 0.5× extra weight.
    boost = 0.0
    if corroboration_map:
        evidence_map = signals.get("_evidence", {}) or {}
        for sig_key, val in public_signals.items():
            if val in (None, [], "", False):
                continue
            ev_id = evidence_map.get(sig_key)
            if ev_id and corroboration_map.get(ev_id, 1) >= 2:
                boost += 0.5
    present_effective = present_raw + boost

    if present_effective < recipe.minimum_required_signals:
        missing = [k for k, v in public_signals.items() if v in (None, [], "", False)]
        return InsufficientSignals(
            metric=recipe.metric,
            method_id=method_id,
            signals_present=int(present_effective),
            signals_required=recipe.minimum_required_signals,
            missing=missing,
        )

    result = impl.compute(signals, recipe)
    if isinstance(result, InsufficientSignals):
        return result

    # A6 — Freshness gate. Recipes set ``minimum_evidence_freshness_days``;
    # when too few signals trace to recent-enough evidence, downgrade the
    # confidence to LOW and append a caveat so the Author / Reviewer can
    # see that the estimate is leaning on stale data. We do NOT prune the
    # signals or refuse to produce an Estimate — the brief still ships,
    # just with an honest confidence label.
    if recipe.minimum_evidence_freshness_days:
        fresh, total = _count_fresh_signals(
            result.signals_used, ledger,
            recipe.minimum_evidence_freshness_days,
        )
        if total > 0 and fresh < recipe.minimum_required_signals:
            stale_caveat = (
                f"Some signals exceed the freshness window of "
                f"{recipe.minimum_evidence_freshness_days} days "
                f"({fresh}/{total} fresh)."
            )
            logger.info(
                "A6 freshness gate: %s — %d/%d signals fresh, "
                "degrading confidence to low",
                method_id, fresh, total,
            )
            result.confidence = ConfidenceLevel.LOW
            if stale_caveat not in result.caveat_text:
                result.caveat_text = (
                    result.caveat_text.rstrip() + " " + stale_caveat
                ).strip()

    return Estimate(
        entity_id=entity_id,
        metric=recipe.metric,
        applies_to=recipe.applies_to,  # type: ignore[arg-type]
        value_range=result.format_value_range(),
        unit=result.unit,
        confidence=result.confidence,
        method_id=method_id,
        signals_used=result.signals_used,
        assumptions=result.assumptions,
        caveat_text=result.caveat_text,
    )


def _count_fresh_signals(
    signals_used: list[SignalUsed],
    ledger: list[VerifiedEvidenceItem],
    freshness_days: int,
) -> tuple[int, int]:
    """Return (fresh_count, total_count) of signals whose underlying
    evidence_id is younger than ``freshness_days``. Signals whose
    evidence_id is not in the ledger are counted as stale (defensive)."""
    if freshness_days <= 0 or not signals_used:
        return len(signals_used), len(signals_used)
    cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
    by_id = {ev.id: ev for ev in ledger}
    fresh = 0
    for sig in signals_used:
        ev = by_id.get(sig.evidence_id)
        if ev is None:
            continue
        if ev.fetched_at >= cutoff:
            fresh += 1
    return fresh, len(signals_used)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_money_range(low: float, high: float, unit: str) -> str:
    """Format a numeric range like (500_000, 2_000_000, 'USD/year') → '$0.5-2M'."""
    if "USD" in unit:
        return f"{_money(low)}-{_money(high)}"
    return f"{low:g}-{high:g}"


def _money(v: float) -> str:
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M".replace(".0M", "M")
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"
