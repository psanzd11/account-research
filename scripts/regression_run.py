"""Standalone runner for the regression set.

Iterates through `tests/regression/golden/*.json`, audits each referenced
brief, prints a per-entity table, and exits non-zero if any threshold fails.

Usage:
    python scripts/regression_run.py

Honors `needs_refresh: true` in a golden spec by labeling that row "SKIP"
without failing the run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow `python scripts/regression_run.py` without manual PYTHONPATH.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from account_research.db import REPO_ROOT  # noqa: E402
from tests.regression.audit import (  # noqa: E402
    audit_brief,
    evaluate,
    load_brief_from_disk,
)


GOLDEN_DIR = ROOT / "tests" / "regression" / "golden"
BRIEFS_DIR = REPO_ROOT / "outputs" / "briefs"


def _print_row(name: str, status: str, metrics: dict | None, failures: list[str]):
    if metrics:
        cells = [
            metrics.get("n_stats"),
            metrics.get("n_industries"),
            metrics.get("n_geographic_footprint"),
            metrics.get("n_sources"),
            f"{metrics.get('pct_text_fields_cited')}%",
            f"{metrics.get('pct_stats_cited')}%",
            f"{metrics.get('pct_badge_with_caveat')}%",
        ]
        cell_str = "  ".join(f"{c!s:>6}" for c in cells)
    else:
        cell_str = " " * 56
    print(f"  {name:<28s} {status:<10s} {cell_str}")
    for f in failures:
        print(f"      ! {f}")


def main() -> int:
    print()
    print(
        f"  {'Entity':<28s} {'Status':<10s} "
        f"{'stats':>6} {'ind':>6} {'geo':>6} {'src':>6} "
        f"{'txt%':>7} {'stat%':>7} {'cav%':>7}"
    )
    print("  " + "-" * 95)

    overall_fail = False
    skipped = 0
    for golden_path in sorted(GOLDEN_DIR.glob("*.json")):
        spec = json.loads(golden_path.read_text(encoding="utf-8"))
        name = spec["entity_name"]
        if spec.get("needs_refresh"):
            _print_row(name, "SKIP", None, [spec.get("needs_refresh_reason", "")])
            skipped += 1
            continue

        brief_path = BRIEFS_DIR / f"{spec['brief_id']}.json"
        if not brief_path.exists():
            overall_fail = True
            _print_row(name, "MISSING", None,
                       [f"brief {spec['brief_id']} not found at {brief_path}"])
            continue

        try:
            brief = load_brief_from_disk(brief_path)
        except Exception as exc:  # noqa: BLE001
            overall_fail = True
            _print_row(name, "ERROR", None, [f"load error: {exc}"])
            continue

        metrics = audit_brief(brief)
        failures = evaluate(metrics, spec["thresholds"])
        status = "PASS" if not failures else "FAIL"
        if failures:
            overall_fail = True
        _print_row(name, status, metrics.to_dict(), failures)

    print()
    print(f"  Result: {'FAILED' if overall_fail else 'PASSED'} "
          f"({skipped} skipped — re-run pipeline to refresh those briefs)")
    print()
    return 1 if overall_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
