"""Compute post-phase metrics from the LLM trace and diff against the baseline.

Reads ``outputs/llm_trace/<today>.jsonl``, filters to calls after a given
start timestamp, sums tokens (including ``cache_read_input_tokens`` and
``cache_creation_input_tokens``) and USD spend, and writes the result to a
``docs/metrics-phase-<n>.json`` file alongside the existing baseline.

Usage::

    python scripts/phase_diff.py \\
        --since 2026-05-18T12:44:51+00:00 \\
        --phase 1 \\
        --baseline docs/metrics-baseline.json \\
        --entity-name "Guillermo Jaime Calderón"

Pure-Python — does not call the API. Designed to be run immediately after
a regression pipeline run completes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PRICING = {
    "claude-sonnet-4-6":         {"in": 3.00, "out": 15.00},
    "claude-opus-4-7":           {"in": 15.00, "out": 75.00},
    "claude-haiku-4-5-20251001": {"in": 1.00, "out": 5.00},
}
# Anthropic ephemeral-cache: cache_read tokens billed at 10% of input rate;
# cache_creation billed at 125% of input rate (5-minute TTL).
CACHE_READ_RATE = 0.10
CACHE_WRITE_RATE = 1.25


def _cost_usd(call: dict) -> float:
    p = PRICING.get(call["model"], {"in": 0, "out": 0})
    in_tok = call.get("input_tokens", 0)
    out_tok = call.get("output_tokens", 0)
    cache_read = call.get("cache_read_input_tokens", 0)
    cache_write = call.get("cache_creation_input_tokens", 0)
    # input_tokens already excludes cache_read / cache_write per Anthropic's
    # accounting (the cache counts are reported separately).
    base = (in_tok * p["in"] + out_tok * p["out"]) / 1_000_000
    cache_cost = (
        cache_read * p["in"] * CACHE_READ_RATE
        + cache_write * p["in"] * CACHE_WRITE_RATE
    ) / 1_000_000
    return base + cache_cost


def collect(trace_dir: Path, since_iso: str) -> dict:
    since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    calls: list[dict] = []
    for f in sorted(trace_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if ts >= since:
                calls.append(r)
    if not calls:
        return {"calls": []}

    total_in = sum(c.get("input_tokens", 0) for c in calls)
    total_out = sum(c.get("output_tokens", 0) for c in calls)
    total_cache_read = sum(c.get("cache_read_input_tokens", 0) for c in calls)
    total_cache_write = sum(c.get("cache_creation_input_tokens", 0) for c in calls)
    total_usd = sum(_cost_usd(c) for c in calls)
    total_elapsed_ms = sum(c.get("elapsed_ms", 0) for c in calls)

    by_agent: dict[str, dict] = {}
    for c in calls:
        a = c["agent"]
        d = by_agent.setdefault(a, {
            "calls": 0, "in_tokens": 0, "out_tokens": 0,
            "cache_read": 0, "cache_write": 0, "usd": 0.0,
            "elapsed_ms": 0.0,
        })
        d["calls"] += 1
        d["in_tokens"] += c.get("input_tokens", 0)
        d["out_tokens"] += c.get("output_tokens", 0)
        d["cache_read"] += c.get("cache_read_input_tokens", 0)
        d["cache_write"] += c.get("cache_creation_input_tokens", 0)
        d["usd"] += _cost_usd(c)
        d["elapsed_ms"] += c.get("elapsed_ms", 0)

    return {
        "calls_total": len(calls),
        "tokens": {
            "prompt": total_in,
            "completion": total_out,
            "cache_read": total_cache_read,
            "cache_creation": total_cache_write,
        },
        "cost_usd_total": round(total_usd, 4),
        "llm_elapsed_ms_total": round(total_elapsed_ms),
        "per_agent": {
            a: {**d, "usd": round(d["usd"], 4), "elapsed_ms": round(d["elapsed_ms"])}
            for a, d in by_agent.items()
        },
        "first_call_ts": calls[0]["ts"],
        "last_call_ts": calls[-1]["ts"],
    }


def diff_against_baseline(post: dict, baseline_path: Path) -> dict:
    base = json.loads(baseline_path.read_text(encoding="utf-8"))
    base_cost = base.get("cost_usd_total", 0.0)
    base_tokens = base.get("tokens", {})
    post_cost = post["cost_usd_total"]
    delta_cost = post_cost - base_cost
    pct = (delta_cost / base_cost * 100.0) if base_cost else 0.0
    return {
        "baseline_cost_usd": base_cost,
        "post_cost_usd": post_cost,
        "delta_cost_usd": round(delta_cost, 4),
        "delta_cost_pct": round(pct, 2),
        "baseline_cache_read": base_tokens.get("cache_read", 0),
        "post_cache_read": post["tokens"]["cache_read"],
        "baseline_prompt_tokens": base_tokens.get("prompt", 0),
        "post_prompt_tokens": post["tokens"]["prompt"],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", required=True,
                   help="ISO-8601 UTC timestamp (e.g. 2026-05-18T12:44:51+00:00)")
    p.add_argument("--phase", type=int, required=True)
    p.add_argument("--baseline", type=Path,
                   default=Path("docs/metrics-baseline.json"))
    p.add_argument("--entity-name", required=True)
    p.add_argument("--trace-dir", type=Path,
                   default=Path("outputs/llm_trace"))
    p.add_argument("--out", type=Path, default=None,
                   help="Output JSON path (default docs/metrics-phase-<n>.json)")
    args = p.parse_args(argv)

    out = args.out or Path(f"docs/metrics-phase-{args.phase}.json")
    post = collect(args.trace_dir, args.since)
    if "calls_total" not in post:
        print("No LLM calls found in the trace since", args.since, file=sys.stderr)
        return 1

    diff = diff_against_baseline(post, args.baseline) if args.baseline.exists() else {}

    record = {
        "captured_at": datetime.utcnow().isoformat() + "+00:00",
        "phase": args.phase,
        "entity_name": args.entity_name,
        "since": args.since,
        **post,
        "diff_vs_baseline": diff,
    }
    out.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    print(json.dumps({"cost_usd_total": post["cost_usd_total"],
                       "cache_read": post["tokens"]["cache_read"],
                       "diff": diff}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
