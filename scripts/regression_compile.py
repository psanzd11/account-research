"""Compile per-entity metrics from the LLM trace + SQLite ledger.

Reads outputs/llm_trace/*.jsonl, slices off the first N rows (pre-regression
cutoff saved by the operator), then groups remaining calls into entity
buckets by joining the call timestamp against the pipeline_runs table's
(started_at, completed_at) window.

Writes docs/metrics-plan-b.json with one bucket per regression entity plus
totals and a diff against docs/metrics-baseline.json.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from account_research.db import SessionLocal  # noqa: E402
from account_research.ledger import EntityRow, EvidenceRow, PipelineRunRow  # noqa: E402
TRACE_DIR = REPO_ROOT / "outputs" / "llm_trace"
CUTOFF_PATH = REPO_ROOT / "outputs" / "_regression_cutoff.json"
BASELINE_PATH = REPO_ROOT / "docs" / "metrics-baseline.json"
OUT_PATH = REPO_ROOT / "docs" / "metrics-plan-b.json"

PRICING = {
    "claude-sonnet-4-6":         {"in": 3.00, "out": 15.00},
    "claude-opus-4-7":           {"in": 15.00, "out": 75.00},
    "claude-haiku-4-5-20251001": {"in": 1.00, "out": 5.00},
}
CACHE_READ_RATE = 0.10
CACHE_WRITE_RATE = 1.25


def _cost_usd(call: dict) -> float:
    p = PRICING.get(call["model"], {"in": 0, "out": 0})
    in_tok = call.get("input_tokens", 0)
    out_tok = call.get("output_tokens", 0)
    cache_read = call.get("cache_read_input_tokens", 0)
    cache_write = call.get("cache_creation_input_tokens", 0)
    base = (in_tok * p["in"] + out_tok * p["out"]) / 1_000_000
    cache_cost = (
        cache_read * p["in"] * CACHE_READ_RATE
        + cache_write * p["in"] * CACHE_WRITE_RATE
    ) / 1_000_000
    return base + cache_cost


def _saved_usd(call: dict) -> float:
    p = PRICING.get(call["model"], {"in": 0})
    cache_read = call.get("cache_read_input_tokens", 0)
    return cache_read * p["in"] * (1.0 - CACHE_READ_RATE) / 1_000_000


def main() -> int:
    cutoff = json.loads(CUTOFF_PATH.read_text(encoding="utf-8"))
    pre_count = cutoff["pre_count"]

    all_rows = []
    for f in sorted(TRACE_DIR.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    all_rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    post_rows = all_rows[pre_count:]
    print(f"Total trace rows: {len(all_rows)}; post-regression: {len(post_rows)}")

    with SessionLocal() as s:
        runs = s.scalars(
            select(PipelineRunRow)
            .where(PipelineRunRow.started_at >= datetime(2026, 5, 18, 15, 0, tzinfo=timezone.utc))
            .order_by(PipelineRunRow.started_at)
        ).all()
        run_data = []
        for r in runs:
            ent = s.get(EntityRow, r.entity_id) if r.entity_id else None
            ev_count = 0
            ev_verified = 0
            ev_dead = 0
            ev_rejected = 0
            ev_unverifiable = 0
            if r.entity_id:
                rows = s.scalars(
                    select(EvidenceRow).where(EvidenceRow.entity_id == r.entity_id)
                ).all()
                for ev in rows:
                    ev_count += 1
                    st = ev.verification_status
                    if st == "verified":
                        ev_verified += 1
                    elif st == "source_dead":
                        ev_dead += 1
                    elif st == "rejected":
                        ev_rejected += 1
                    elif st == "unverifiable":
                        ev_unverifiable += 1
            # DB stores naive UTC datetimes; force tz=UTC for comparison.
            started = r.started_at.replace(tzinfo=timezone.utc) if r.started_at else None
            completed = r.completed_at.replace(tzinfo=timezone.utc) if r.completed_at else None
            run_data.append({
                "run_id": str(r.id),
                "query": r.query,
                "entity_id": str(r.entity_id) if r.entity_id else None,
                "entity_name": ent.name if ent else None,
                "entity_type": ent.type if ent else None,
                "status": r.status,
                "started_at": started.isoformat() if started else None,
                "completed_at": completed.isoformat() if completed else None,
                "iterations": r.iterations,
                "evidence_total": ev_count,
                "evidence_verified": ev_verified,
                "evidence_dead": ev_dead,
                "evidence_rejected": ev_rejected,
                "evidence_unverifiable": ev_unverifiable,
                "verification_rate_pct": round(100.0 * ev_verified / max(ev_count, 1), 1),
            })

    def _parse_ts(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    # Bucket trace calls into the run windows.
    buckets: dict[str, list] = {r["run_id"]: [] for r in run_data}
    bucket_other = []
    for call in post_rows:
        try:
            ts = _parse_ts(call["ts"])
        except (KeyError, ValueError):
            continue
        assigned = False
        for r in run_data:
            if not r["started_at"] or not r["completed_at"]:
                continue
            start = _parse_ts(r["started_at"])
            end = _parse_ts(r["completed_at"])
            if start <= ts <= end:
                buckets[r["run_id"]].append(call)
                assigned = True
                break
        if not assigned:
            bucket_other.append(call)

    # Per-entity aggregates.
    summary = []
    grand_in = grand_out = grand_cache_r = grand_cache_w = 0
    grand_usd = grand_saved = 0.0
    for r in run_data:
        calls = buckets[r["run_id"]]
        in_tok = sum(c.get("input_tokens", 0) for c in calls)
        out_tok = sum(c.get("output_tokens", 0) for c in calls)
        cache_r = sum(c.get("cache_read_input_tokens", 0) for c in calls)
        cache_w = sum(c.get("cache_creation_input_tokens", 0) for c in calls)
        usd = sum(_cost_usd(c) for c in calls)
        saved = sum(_saved_usd(c) for c in calls)
        wall = None
        if r["started_at"] and r["completed_at"]:
            wall = (_parse_ts(r["completed_at"]) - _parse_ts(r["started_at"])).total_seconds()
        per_agent = {}
        for c in calls:
            a = c["agent"]
            d = per_agent.setdefault(a, {"calls": 0, "usd": 0.0, "in": 0, "out": 0, "cache_r": 0, "cache_w": 0})
            d["calls"] += 1
            d["in"] += c.get("input_tokens", 0)
            d["out"] += c.get("output_tokens", 0)
            d["cache_r"] += c.get("cache_read_input_tokens", 0)
            d["cache_w"] += c.get("cache_creation_input_tokens", 0)
            d["usd"] += _cost_usd(c)
        for d in per_agent.values():
            d["usd"] = round(d["usd"], 4)
        summary.append({
            **r,
            "calls_total": len(calls),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_tokens": cache_r,
            "cache_creation_tokens": cache_w,
            "cost_usd": round(usd, 4),
            "saved_usd": round(saved, 4),
            "wall_seconds": wall,
            "per_agent": per_agent,
        })
        grand_in += in_tok
        grand_out += out_tok
        grand_cache_r += cache_r
        grand_cache_w += cache_w
        grand_usd += usd
        grand_saved += saved

    # Baseline diff (per-entity if baseline matches name; otherwise totals only).
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8")) if BASELINE_PATH.exists() else {}
    base_cost = baseline.get("cost_usd_total", 0.0)
    base_entity = (baseline.get("entity") or {}).get("name")

    record = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "phase": "plan-b",
        "regression_entities": summary,
        "uncategorized_calls": len(bucket_other),
        "grand_totals": {
            "calls": sum(s_["calls_total"] for s_ in summary),
            "input_tokens": grand_in,
            "output_tokens": grand_out,
            "cache_read_tokens": grand_cache_r,
            "cache_creation_tokens": grand_cache_w,
            "cost_usd": round(grand_usd, 4),
            "saved_usd": round(grand_saved, 4),
        },
        "diff_vs_baseline": {
            "baseline_entity": base_entity,
            "baseline_cost_usd": base_cost,
            "matched_entity_cost_usd": next(
                (s_["cost_usd"] for s_ in summary if base_entity and s_["entity_name"] == base_entity),
                None,
            ),
        },
    }
    OUT_PATH.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH}")

    print("\nPer-entity summary:")
    print(f"  {'entity':<30} {'calls':>5} {'cost':>9} {'saved':>8} {'wall':>6} {'iter':>4} {'verif':>6}")
    for s_ in summary:
        ent = (s_["entity_name"] or s_["query"])[:30]
        print(
            f"  {ent:<30} {s_['calls_total']:>5} "
            f"${s_['cost_usd']:>7.2f} ${s_['saved_usd']:>6.2f} "
            f"{int(s_['wall_seconds'] or 0):>6}s {s_['iterations']:>4} "
            f"{s_['verification_rate_pct']:>5.1f}%"
        )
    print(f"  {'TOTAL':<30} {record['grand_totals']['calls']:>5} "
          f"${record['grand_totals']['cost_usd']:>7.2f} "
          f"${record['grand_totals']['saved_usd']:>6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
