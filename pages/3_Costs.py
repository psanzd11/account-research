"""Costs page — aggregate USD spend from the LLM trace."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.ui import (
    empty_state,
    page_header,
    section_label,
    setup_page,
    sidebar_section,
)


TRACE_DIR = REPO_ROOT / "outputs" / "llm_trace"

PRICING = {
    "claude-sonnet-4-6":           {"in": 3.00,  "out": 15.00},
    "claude-opus-4-7":             {"in": 15.00, "out": 75.00},
    "claude-haiku-4-5-20251001":   {"in": 1.00,  "out": 5.00},
}

# Anthropic prompt-cache multipliers (per https://docs.anthropic.com/.../prompt-caching):
#  - cache write (creation): 1.25× the model's input price
#  - cache read:              0.10× the model's input price
# We compute fresh-input cost as (input_tokens - cache_read - cache_creation) × in,
# then add cache_read × in × 0.10 and cache_creation × in × 1.25 separately. The
# "savings" line is the counterfactual: what cache_read tokens would have cost
# without caching, minus what they actually cost.
CACHE_READ_MULT = 0.10
CACHE_WRITE_MULT = 1.25

AGENT_LABELS = {
    "anthropic_web_fetch": "Web fetch (Haiku)",
    "anthropic_web_search": "Web search",
}


setup_page("Costs")
page_header(
    "Costs",
    "Per-call breakdown of every Anthropic API request the pipeline has made.",
)


@st.cache_data(ttl=15)
def load_trace() -> pd.DataFrame:
    rows = []
    if not TRACE_DIR.exists():
        return pd.DataFrame()
    for f in sorted(TRACE_DIR.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append(r)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df["date"] = df["ts"].dt.date
    df["input_tokens"] = df["input_tokens"].fillna(0).astype(int)
    df["output_tokens"] = df["output_tokens"].fillna(0).astype(int)
    # Older trace rows pre-date prompt caching; default missing columns to 0.
    for col in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if col not in df.columns:
            df[col] = 0
        df[col] = df[col].fillna(0).astype(int)

    def _cost(row):
        p = PRICING.get(row["model"])
        if not p:
            return 0.0
        cache_read = row["cache_read_input_tokens"]
        cache_write = row["cache_creation_input_tokens"]
        # input_tokens from Anthropic is the *fresh* (uncached) input only —
        # cache_read and cache_creation are reported separately.
        fresh_in = row["input_tokens"]
        in_cost = fresh_in * p["in"]
        cache_read_cost = cache_read * p["in"] * CACHE_READ_MULT
        cache_write_cost = cache_write * p["in"] * CACHE_WRITE_MULT
        out_cost = row["output_tokens"] * p["out"]
        return (in_cost + cache_read_cost + cache_write_cost + out_cost) / 1_000_000

    def _saved(row):
        p = PRICING.get(row["model"])
        if not p:
            return 0.0
        # Counterfactual: cache_read tokens billed at full price instead of 10%.
        cache_read = row["cache_read_input_tokens"]
        return cache_read * p["in"] * (1.0 - CACHE_READ_MULT) / 1_000_000

    df["usd"] = df.apply(_cost, axis=1)
    df["usd_saved"] = df.apply(_saved, axis=1)
    df["agent_label"] = df["agent"].map(lambda a: AGENT_LABELS.get(a, a))
    return df


df = load_trace()
if df.empty:
    empty_state(
        "No LLM trace yet",
        "Once you run the pipeline, every Anthropic API call will be logged "
        "to `outputs/llm_trace/<date>.jsonl` and aggregated here.",
    )
    st.stop()

with st.sidebar:
    st.markdown("---")
    sidebar_section("Filters")
    min_d, max_d = df["date"].min(), df["date"].max()
    date_range = st.date_input(
        "Date range",
        value=(min_d, max_d),
        min_value=min_d,
        max_value=max_d,
    )

if isinstance(date_range, tuple) and len(date_range) == 2:
    d0, d1 = date_range
    df = df[(df["date"] >= d0) & (df["date"] <= d1)]


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


total_calls = len(df)
total_in = int(df["input_tokens"].sum())
total_out = int(df["output_tokens"].sum())
total_cache_read = int(df["cache_read_input_tokens"].sum())
total_cache_write = int(df["cache_creation_input_tokens"].sum())
total_usd = float(df["usd"].sum())
total_saved = float(df["usd_saved"].sum())
errors = int(df["error"].notna().sum()) if "error" in df.columns else 0

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total calls", f"{total_calls:,}")
c2.metric(
    "Input tokens",
    _fmt_tokens(total_in + total_cache_read + total_cache_write),
    delta=(
        f"cache: {_fmt_tokens(total_cache_read)} read / {_fmt_tokens(total_cache_write)} write"
        if (total_cache_read or total_cache_write) else None
    ),
    delta_color="off",
)
c3.metric("Output tokens", _fmt_tokens(total_out))
c4.metric(
    "Spend",
    f"${total_usd:,.2f}",
    delta=(
        f"saved ${total_saved:,.2f} via cache"
        if total_saved > 0
        else f"≈ ${total_usd / max(total_calls, 1):.2f} / call"
    ),
    delta_color="normal" if total_saved > 0 else "off",
)
c5.metric("Errors", f"{errors:,}", delta="all green" if errors == 0 else "needs attention")

with st.expander("Pricing assumptions (per million tokens)"):
    st.json(PRICING)
    st.caption(
        f"Prompt cache multipliers — read: {CACHE_READ_MULT:.0%} of input price, "
        f"write/creation: {CACHE_WRITE_MULT:.0%} of input price. Fresh input "
        "tokens (the `input_tokens` field from Anthropic) are billed at the "
        "base rate; `cache_read_input_tokens` and `cache_creation_input_tokens` "
        "are billed at the multipliers above."
    )

section_label("Spend by day")
by_day = df.groupby("date").agg(
    calls=("call_id", "count"),
    in_tokens=("input_tokens", "sum"),
    out_tokens=("output_tokens", "sum"),
    cache_read=("cache_read_input_tokens", "sum"),
    cache_write=("cache_creation_input_tokens", "sum"),
    usd=("usd", "sum"),
    saved=("usd_saved", "sum"),
).reset_index().sort_values("date")

cA, cB = st.columns([2, 1])

if not by_day.empty:
    chart_df = by_day.copy()
    chart_df["date"] = pd.to_datetime(chart_df["date"])
    cA.bar_chart(
        chart_df.set_index("date")["usd"],
        y_label="USD",
        color="#E8473F",
    )
else:
    cA.info("No spend in this date range.")

cB.dataframe(
    by_day.rename(columns={
        "date": "Date",
        "calls": "Calls",
        "in_tokens": "Input Tokens",
        "out_tokens": "Output Tokens",
        "cache_read": "Cache Read",
        "cache_write": "Cache Write",
        "usd": "Cost",
        "saved": "Saved",
    }),
    width="stretch",
    hide_index=True,
    column_config={
        "Cost": st.column_config.NumberColumn("Cost", format="$%.2f"),
        "Saved": st.column_config.NumberColumn("Saved", format="$%.2f"),
        "Date": st.column_config.DateColumn("Date", format="MMM D"),
    },
)

section_label("Breakdown")


def _with_total(grouped: pd.DataFrame, label_col: str) -> pd.DataFrame:
    total = pd.DataFrame([{
        label_col: "Total",
        "calls": int(grouped["calls"].sum()),
        "in_tokens": int(grouped["in_tokens"].sum()),
        "out_tokens": int(grouped["out_tokens"].sum()),
        "cache_read": int(grouped["cache_read"].sum()),
        "cache_write": int(grouped["cache_write"].sum()),
        "usd": float(grouped["usd"].sum()),
        "saved": float(grouped["saved"].sum()),
    }])
    return pd.concat([grouped, total], ignore_index=True)


def _rename_breakdown(df_: pd.DataFrame, label_col: str, label_display: str) -> pd.DataFrame:
    return df_.rename(columns={
        label_col: label_display,
        "calls": "Calls",
        "in_tokens": "Input Tokens",
        "out_tokens": "Output Tokens",
        "cache_read": "Cache Read",
        "cache_write": "Cache Write",
        "usd": "Cost",
        "saved": "Saved",
    })


ca, cm = st.columns(2)

with ca:
    st.markdown("**By agent**")
    by_agent = df.groupby("agent_label").agg(
        calls=("call_id", "count"),
        in_tokens=("input_tokens", "sum"),
        out_tokens=("output_tokens", "sum"),
        cache_read=("cache_read_input_tokens", "sum"),
        cache_write=("cache_creation_input_tokens", "sum"),
        usd=("usd", "sum"),
        saved=("usd_saved", "sum"),
    ).reset_index().sort_values("usd", ascending=False)

    if not by_agent.empty:
        donut = (
            alt.Chart(by_agent)
            .mark_arc(innerRadius=55, outerRadius=95)
            .encode(
                theta=alt.Theta("usd:Q"),
                color=alt.Color(
                    "agent_label:N",
                    title="Agent",
                    scale=alt.Scale(scheme="reds"),
                ),
                tooltip=[
                    alt.Tooltip("agent_label:N", title="Agent"),
                    alt.Tooltip("calls:Q", title="Calls"),
                    alt.Tooltip("usd:Q", title="Cost", format="$,.2f"),
                ],
            )
            .properties(height=220)
        )
        st.altair_chart(donut, width="stretch")

    st.dataframe(
        _rename_breakdown(_with_total(by_agent, "agent_label"), "agent_label", "Agent"),
        width="stretch",
        hide_index=True,
        column_config={
            "Cost": st.column_config.NumberColumn("Cost", format="$%.2f"),
            "Saved": st.column_config.NumberColumn("Saved", format="$%.2f"),
        },
    )

with cm:
    st.markdown("**By model**")
    by_model = df.groupby("model").agg(
        calls=("call_id", "count"),
        in_tokens=("input_tokens", "sum"),
        out_tokens=("output_tokens", "sum"),
        cache_read=("cache_read_input_tokens", "sum"),
        cache_write=("cache_creation_input_tokens", "sum"),
        usd=("usd", "sum"),
        saved=("usd_saved", "sum"),
    ).reset_index().sort_values("usd", ascending=False)

    if not by_model.empty:
        donut_m = (
            alt.Chart(by_model)
            .mark_arc(innerRadius=55, outerRadius=95)
            .encode(
                theta=alt.Theta("usd:Q"),
                color=alt.Color(
                    "model:N",
                    title="Model",
                    scale=alt.Scale(scheme="oranges"),
                ),
                tooltip=[
                    alt.Tooltip("model:N", title="Model"),
                    alt.Tooltip("calls:Q", title="Calls"),
                    alt.Tooltip("usd:Q", title="Cost", format="$,.2f"),
                ],
            )
            .properties(height=220)
        )
        st.altair_chart(donut_m, width="stretch")

    st.dataframe(
        _rename_breakdown(_with_total(by_model, "model"), "model", "Model"),
        width="stretch",
        hide_index=True,
        column_config={
            "Cost": st.column_config.NumberColumn("Cost", format="$%.2f"),
            "Saved": st.column_config.NumberColumn("Saved", format="$%.2f"),
        },
    )

st.divider()
section_label("Every call")
with st.expander(f"Show all {total_calls:,} calls", expanded=False):
    sort_col = st.selectbox(
        "Sort by",
        ["ts", "usd", "elapsed_ms", "output_tokens"],
        index=0,
    )
    ascending = st.checkbox("Ascending", value=False)
    show = df.sort_values(sort_col, ascending=ascending)
    display_cols = [
        "ts", "agent_label", "model", "input_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
        "output_tokens", "usd", "elapsed_ms", "stop_reason", "error",
    ]
    display_cols = [c for c in display_cols if c in show.columns]
    st.dataframe(
        show[display_cols].rename(columns={
            "ts": "When",
            "agent_label": "Agent",
            "model": "Model",
            "input_tokens": "Input Tokens",
            "cache_read_input_tokens": "Cache Read",
            "cache_creation_input_tokens": "Cache Write",
            "output_tokens": "Output Tokens",
            "usd": "Cost",
            "elapsed_ms": "Elapsed (ms)",
            "stop_reason": "Stop reason",
            "error": "Error",
        }),
        width="stretch",
        hide_index=True,
        column_config={
            "Cost": st.column_config.NumberColumn("Cost", format="$%.3f"),
            "Elapsed (ms)": st.column_config.NumberColumn("Elapsed (ms)", format="%.0f"),
            "When": st.column_config.DatetimeColumn("When", format="MMM D, HH:mm:ss"),
        },
    )
