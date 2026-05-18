"""Sprint 2 — regression harness over the 6 fixed regression entities.

For each golden spec under `tests/regression/golden/*.json`:
  1. Load the referenced brief JSON from `outputs/briefs/<brief_id>.json`.
  2. Audit it with `tests.regression.audit.audit_brief`.
  3. Assert every threshold in the golden spec is met.

Goldens flagged with `needs_refresh: true` are skipped — the brief was
generated before a schema tightening (or before a Sprint 1 fix) and must be
regenerated from a fresh pipeline run. The harness still detects them so
the dev knows to refresh.

Run with:
    pytest tests/regression -v
or via the standalone runner:
    python scripts/regression_run.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from account_research.db import REPO_ROOT
from tests.regression.audit import (
    audit_brief,
    evaluate,
    load_brief_from_disk,
)


GOLDEN_DIR = Path(__file__).parent / "golden"
BRIEFS_DIR = REPO_ROOT / "outputs" / "briefs"


def _golden_files() -> list[Path]:
    return sorted(GOLDEN_DIR.glob("*.json"))


def _id_for(path: Path) -> str:
    return path.stem  # e.g. "stripe", "mercadolibre"


@pytest.mark.parametrize("golden_path", _golden_files(), ids=_id_for)
def test_brief_meets_thresholds(golden_path: Path):
    spec = json.loads(golden_path.read_text(encoding="utf-8"))
    if spec.get("needs_refresh"):
        pytest.skip(
            f"{spec['entity_name']} needs refresh: "
            f"{spec.get('needs_refresh_reason', '(no reason given)')}"
        )

    brief_path = BRIEFS_DIR / f"{spec['brief_id']}.json"
    if not brief_path.exists():
        pytest.fail(
            f"Brief {spec['brief_id']} not found at {brief_path}. "
            f"Run pipeline for {spec['entity_name']!r} to generate."
        )

    brief = load_brief_from_disk(brief_path)
    metrics = audit_brief(brief)
    failures = evaluate(metrics, spec["thresholds"])
    assert not failures, (
        f"Regression metrics for {spec['entity_name']} fall below thresholds:\n"
        + "\n".join(f"  - {f}" for f in failures)
        + f"\n\nMetrics: {metrics.to_dict()}"
    )


def test_regression_set_has_six_entities():
    """Per memory `regression-set.md`, the set is exactly 6 entities."""
    assert len(_golden_files()) == 6, (
        f"Expected 6 golden files, found {len(_golden_files())}: "
        f"{[p.name for p in _golden_files()]}"
    )
