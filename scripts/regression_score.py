"""Run the pipeline against the ground-truth regression set and score it.

Usage:
    py scripts/regression_score.py            # run all entities (full pipeline)
    py scripts/regression_score.py --skip-run # score whatever's in the DB already
    py scripts/regression_score.py --only stripe,galperin

Outputs:
    outputs/regression/<date>_<entity>.json    raw metrics per entity
    outputs/regression/<date>_summary.md       human-readable rollup

Acceptance bars (from Round 2 plan):
    - hero badge confidence ≥ medium in ≥70% of entities
    - ≥2 Tier-1 sources per required category
    - aggregator share ≤30%
    - verification rate ≥65%
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from uuid import UUID

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from account_research.agents.source_authority import (
    aggregator_share,
    tier1_count_per_category,
    tier_breakdown,
)
from account_research.db import SessionLocal
from account_research.ledger import EntityRow, EstimateRow, EvidenceRow

GROUND_TRUTH_PATH = REPO_ROOT / "tests" / "regression" / "ground_truth.yaml"
OUTPUT_DIR = REPO_ROOT / "outputs" / "regression"


def load_ground_truth() -> list[dict]:
    data = yaml.safe_load(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
    return data["entities"]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def find_entity_by_name(name: str) -> UUID | None:
    """Match by normalized case-insensitive substring of the canonical name.
    Returns the MOST RECENTLY CREATED entity when multiple match — necessary
    when the same name has been processed across pipeline versions.

    Normalization strips suffixes (Inc, LLC, S.A.), whitespace, and common
    punctuation so 'Mercado Libre' matches 'MercadoLibre, Inc.'.
    """
    def _norm(s: str) -> str:
        s = s.lower().strip()
        for suffix in (", inc.", ", inc", " inc.", " inc",
                       ", llc", " llc",
                       " s.a.", " sa", " sas",
                       " corp.", " corp",
                       " ltd.", " ltd"):
            if s.endswith(suffix):
                s = s[: -len(suffix)].strip()
        # Strip remaining commas/dots and collapse whitespace
        s = s.replace(",", "").replace(".", "")
        s = " ".join(s.split())
        # Also try the no-space variant for compound names
        return s

    def _matches(needle: str, candidate: str) -> bool:
        if needle in candidate or candidate in needle:
            return True
        # Also try no-space comparison: "mercado libre" ↔ "mercadolibre"
        ns = needle.replace(" ", "")
        cs = candidate.replace(" ", "")
        return ns in cs or cs in ns

    needle = _norm(name)
    with SessionLocal() as s:
        rows = (
            s.query(EntityRow)
            .order_by(EntityRow.created_at.desc())
            .all()
        )
        for row in rows:
            if _matches(needle, _norm(row.name)):
                return row.id
    return None


def score_entity(entity_meta: dict) -> dict:
    """Score one ground-truth entity against the DB ledger."""
    entity_id = find_entity_by_name(entity_meta["name"])
    if entity_id is None:
        return {
            "name": entity_meta["name"],
            "status": "missing",
            "reason": "Entity not found in DB. Run the pipeline first.",
        }

    with SessionLocal() as s:
        all_items = s.query(EvidenceRow).filter(
            EvidenceRow.entity_id == entity_id,
        ).all()
        estimates = (
            s.query(EstimateRow)
            .filter(EstimateRow.entity_id == entity_id)
            .order_by(EstimateRow.computed_at.desc())
            .all()
        )

    if not all_items:
        return {
            "name": entity_meta["name"],
            "status": "empty_ledger",
        }

    # Build VerifiedEvidenceItem-shaped wrappers so the source_authority
    # helpers can run on the raw DB rows.
    class _Row:
        def __init__(self, r):
            self.source_url = r.source_url
            self.source_type = type(  # mimic the enum interface
                "ST", (), {"value": r.source_type, "__eq__": lambda s, o: r.source_type == getattr(o, "value", o)},
            )()
            # Coerce source_type to the actual enum for tier_of compatibility
            from account_research.schemas.evidence import SourceType
            try:
                self.source_type = SourceType(r.source_type)
            except ValueError:
                self.source_type = SourceType.OTHER
            from account_research.schemas.evidence import EvidenceCategory
            try:
                self.category = EvidenceCategory(r.category)
            except ValueError:
                self.category = EvidenceCategory.OTHER

            class _Ver:
                pass
            self.verification = _Ver()
            self.verification.status = r.verification_status or "unverifiable"

    wrappers = [_Row(r) for r in all_items]
    verified = [w for w in wrappers if w.verification.status == "verified"]

    agg_share = aggregator_share(wrappers)
    tiers = tier_breakdown(wrappers)
    tier1_by_cat = tier1_count_per_category(wrappers)
    verification_rate = len(verified) / len(all_items) if all_items else 0.0

    expected = entity_meta["expected"]

    # Estimate analysis
    primary_est = None
    if entity_meta["entity_type"] == "company":
        prefer_methods = ["public_disclosure_v1", "saas_revenue_v1",
                          "consulting_firm_revenue_v1"]
    else:
        prefer_methods = ["net_worth_individual_v1"]
    for m in prefer_methods:
        for e in estimates:
            if e.method_id == m:
                primary_est = e
                break
        if primary_est is not None:
            break

    est_block: dict = {"method_id": None, "value_range": None,
                       "confidence": None, "within_expected": None}
    if primary_est is not None:
        est_block["method_id"] = primary_est.method_id
        est_block["value_range"] = primary_est.value_range
        est_block["confidence"] = primary_est.confidence

        # Parse value_range like "$5.4M-$37.5M" → (low, high) in USD
        parsed = _parse_money_range(primary_est.value_range)
        est_block["parsed_low_high_usd"] = parsed

        exp_range = expected.get("revenue_range_usd") or expected.get(
            "net_worth_range_usd"
        )
        if parsed and exp_range:
            low, high = parsed
            est_block["within_expected"] = (
                low <= exp_range[1] and high >= exp_range[0]
            )

    # Acceptance gates
    bar_aggregator = agg_share <= expected.get("aggregator_share_max", 0.30)
    bar_verification = verification_rate >= expected.get("verification_rate_min", 0.65)
    bar_tier1_total = tiers.get(1, 0) >= expected.get("tier1_sources_minimum", 2)
    bar_confidence = _confidence_at_least(
        est_block.get("confidence"),
        expected.get("revenue_confidence_min")
        or expected.get("net_worth_confidence_min", "low"),
    )
    # Reviewer-rigor-aligned: ≥2 Tier-1 per required category (the user's
    # actual success bar).
    required_cats = ("company_facts", "financial", "leadership", "products",
                     "clients", "geography")
    weak_per_cat = [
        c for c in required_cats
        if tier1_by_cat.get(c, 0) < 2
    ]
    bar_tier1_per_category = len(weak_per_cat) == 0

    return {
        "name": entity_meta["name"],
        "entity_id": str(entity_id),
        "status": "scored",
        "metrics": {
            "ledger_size": len(all_items),
            "verified": len(verified),
            "verification_rate": round(verification_rate, 3),
            "aggregator_share": round(agg_share, 3),
            "tier_breakdown": {1: tiers.get(1, 0), 2: tiers.get(2, 0), 3: tiers.get(3, 0)},
            "tier1_by_category": tier1_by_cat,
        },
        "estimate": est_block,
        "bars": {
            "aggregator_share_ok": bool(bar_aggregator),
            "verification_rate_ok": bool(bar_verification),
            "tier1_total_ok": bool(bar_tier1_total),
            "tier1_per_category_ok": bool(bar_tier1_per_category),
            "estimate_confidence_ok": bool(bar_confidence),
            "estimate_within_range": est_block.get("within_expected"),
        },
        "weak_categories": weak_per_cat,
        "expected": expected,
    }


def _confidence_at_least(actual: str | None, expected_min: str) -> bool:
    rank = {"unknown": 0, "low": 1, "medium": 2, "high": 3, "verified": 4}
    if actual is None:
        return False
    return rank.get(actual, 0) >= rank.get(expected_min, 0)


_MONEY_NUM = re.compile(r"\$([\d.]+)\s*([KMB])?", re.IGNORECASE)


def _parse_money_range(s: str | None) -> tuple[float, float] | None:
    """Parse '$5.4M-$37.5M' or '$5B-$20B' → (low_usd, high_usd)."""
    if not s:
        return None
    matches = _MONEY_NUM.findall(s)
    if len(matches) < 2:
        return None
    vals: list[float] = []
    for amount, unit in matches[:2]:
        v = float(amount)
        u = (unit or "").upper()
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(u, 1.0)
        vals.append(v * mult)
    return (vals[0], vals[1])


def run_pipeline(entity_meta: dict) -> int:
    """Run the full pipeline for one entity. Returns exit code."""
    cmd = [
        sys.executable, "-m", "account_research.cli", "run",
        entity_meta["query"],
        "--entity-type", entity_meta["entity_type"],
        "-v",
    ]
    if entity_meta.get("geo"):
        cmd.extend(["--geo", entity_meta["geo"]])
    print(f"=== Running pipeline: {entity_meta['name']} ===")
    print(" ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return proc.returncode


def write_report(results: list[dict]) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    # raw JSON
    for r in results:
        slug = _slug(r["name"])
        (OUTPUT_DIR / f"{today}_{slug}.json").write_text(
            json.dumps(r, indent=2, default=str), encoding="utf-8",
        )
    # markdown summary
    lines = [f"# Regression score — {today}\n"]
    lines.append("| Entity | LedgerSize | Verif% | Agg% | T1 | EstConf | EstInRange | Bars |")
    lines.append("|---|---:|---:|---:|---:|---|---|---|")

    bars_passed = 0
    bars_total = 0
    for r in results:
        if r["status"] != "scored":
            lines.append(f"| {r['name']} | — | — | — | — | — | — | **{r['status']}** |")
            continue
        m = r["metrics"]
        e = r["estimate"]
        bars = r["bars"]
        bar_str = "/".join(["Y" if v else "N" for v in bars.values()])
        for v in bars.values():
            if v is None:
                continue
            bars_total += 1
            if v:
                bars_passed += 1
        lines.append(
            f"| {r['name']} | {m['ledger_size']} | "
            f"{int(m['verification_rate']*100)}% | "
            f"{int(m['aggregator_share']*100)}% | "
            f"T1={m['tier_breakdown'][1]} | "
            f"{e['confidence']} | "
            f"{'Y' if bars['estimate_within_range'] else 'N' if bars['estimate_within_range'] is False else '—'} | "
            f"{bar_str} |"
        )

    if bars_total:
        rate = bars_passed / bars_total
        lines.append(f"\n**Overall bar pass rate: {bars_passed}/{bars_total} = {rate*100:.0f}%**")

    md_path = OUTPUT_DIR / f"{today}_summary.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-run", action="store_true",
                    help="Score whatever is already in the DB (don't re-run pipeline)")
    ap.add_argument("--only", default="",
                    help="Comma-separated entity slugs to include (e.g. stripe,galperin)")
    args = ap.parse_args()

    entities = load_ground_truth()
    if args.only:
        wanted = {s.strip().lower() for s in args.only.split(",") if s.strip()}
        entities = [e for e in entities if any(w in _slug(e["name"]) for w in wanted)]
        if not entities:
            print(f"No entities matched --only={args.only}")
            return 2

    if not args.skip_run:
        for e in entities:
            run_pipeline(e)

    results = [score_entity(e) for e in entities]
    md = write_report(results)
    print(f"\nReport: {md}")
    for r in results:
        print(f"  {r['name']}: {r['status']}")
        if r["status"] == "scored":
            for k, v in r["bars"].items():
                print(f"    {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
