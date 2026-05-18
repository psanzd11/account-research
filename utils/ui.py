"""Shared UI helpers for the Streamlit app.

Single entry point: `setup_page(title)` — call once at the top of every page
to apply the design system (page config + global styles + sidebar brand).
"""
from __future__ import annotations

import re
from datetime import datetime

import streamlit as st


def inject_global_styles() -> None:
    st.html(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined&display=block');
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

        /* ---- Material Icons fix (Streamlit chevrons, collapse buttons) ---- */
        .material-symbols-outlined,
        [data-testid="stSidebarCollapseButton"] span,
        [data-testid="stExpanderToggleIcon"] span,
        .ed4y4ls0 {{
            font-family: 'Material Symbols Outlined' !important;
            font-weight: normal;
            font-style: normal;
            font-size: 20px;
            line-height: 1;
            letter-spacing: normal;
            text-transform: none;
            display: inline-block;
            white-space: nowrap;
            word-wrap: normal;
            direction: ltr;
            -webkit-font-feature-settings: 'liga';
            font-feature-settings: 'liga';
            -webkit-font-smoothing: antialiased;
        }}

        /* ---- Hide Streamlit chrome ----
         * Only hide the "Made with Streamlit" footer. Leave the header
         * and toolbar fully intact — they host the sidebar collapse/expand
         * button. Background is left as Streamlit's default; the brand
         * pill in the sidebar establishes ours.
         */
        footer {{ visibility: hidden; }}

        /* ---- Typography ---- */
        html, body, [class*="st-"] {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }}

        /* ---- Layout ---- */
        .block-container {{
            padding: 2.5rem 3rem 4rem 3rem;
            max-width: 1440px;
        }}

        /* ---- Sidebar ---- */
        [data-testid="stSidebar"] {{
            background: #111827;
            border-right: 1px solid rgba(255,255,255,0.06);
        }}
        [data-testid="stSidebar"] * {{ color: #E5E7EB; }}
        [data-testid="stSidebar"] h1,
        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3 {{ color: #FFFFFF !important; }}
        [data-testid="stSidebar"] hr {{
            border-color: rgba(255,255,255,0.08);
            margin: 0.9rem 0;
        }}
        [data-testid="stSidebar"] label {{
            color: #9CA3AF !important;
            font-size: 0.7rem !important;
            font-weight: 600 !important;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }}
        [data-testid="stSidebar"] .stCheckbox label {{
            text-transform: none;
            letter-spacing: 0;
            font-size: 0.875rem !important;
            color: #E5E7EB !important;
            font-weight: 500 !important;
        }}
        [data-testid="stSidebar"] [data-baseweb="tag"] {{
            background-color: #374151 !important;
            color: #FFFFFF !important;
            border-radius: 999px !important;
        }}

        /* ---- Sidebar nav (multipage links) ---- */
        [data-testid="stSidebarNav"] ul {{
            padding: 0.25rem 0.5rem;
            list-style: none;
        }}
        [data-testid="stSidebarNav"] a {{
            padding: 0.45rem 0.75rem;
            border-radius: 6px;
            font-size: 0.875rem;
            font-weight: 500;
            color: rgba(255,255,255,0.55) !important;
            text-decoration: none;
            display: block;
            transition: background 0.12s, color 0.12s;
        }}
        [data-testid="stSidebarNav"] a:hover {{
            background: rgba(255,255,255,0.06);
            color: rgba(255,255,255,0.95) !important;
        }}
        [data-testid="stSidebarNav"] li[aria-selected="true"] a,
        [data-testid="stSidebarNav"] a[aria-current="page"] {{
            background: rgba(255,255,255,0.10);
            color: #FFFFFF !important;
            font-weight: 600;
        }}

        /* ---- Collapse button ---- */
        [data-testid="stSidebarCollapseButton"] button {{
            background: transparent;
            border: none;
            padding: 4px;
            color: rgba(255,255,255,0.5);
        }}
        [data-testid="stSidebarCollapseButton"] button:hover {{
            background: rgba(255,255,255,0.08);
            color: rgba(255,255,255,0.9);
            border-radius: 6px;
        }}

        /* ---- Brand block ---- */
        .ar-brand {{
            display: flex; align-items: center; gap: 0.65rem;
            padding: 0.25rem 0 1.1rem 0;
        }}
        .ar-brand-mark {{
            width: 34px; height: 34px; border-radius: 8px;
            background: linear-gradient(135deg, #E8473F 0%, #F59E0B 100%);
            display: flex; align-items: center; justify-content: center;
            font-weight: 700; color: #FFFFFF;
            font-size: 0.95rem; letter-spacing: -0.02em;
        }}
        .ar-brand-name {{
            color: #FFFFFF !important; font-weight: 700;
            font-size: 1.0rem; letter-spacing: -0.01em; line-height: 1.1;
        }}
        .ar-brand-tag {{
            color: #9CA3AF !important; font-size: 0.65rem;
            text-transform: uppercase; letter-spacing: 0.08em;
        }}
        .ar-sb-section {{
            color: #9CA3AF !important; font-size: 0.68rem;
            text-transform: uppercase; letter-spacing: 0.08em;
            margin: 0.4rem 0 0.4rem 0; font-weight: 600;
        }}

        /* ---- Headings ---- */
        h1 {{
            font-size: 1.5rem !important; font-weight: 700 !important;
            color: #111827 !important; letter-spacing: -0.01em !important;
            margin-bottom: 0.15rem !important;
        }}
        h2 {{
            font-size: 1.05rem !important; font-weight: 600 !important;
            color: #374151 !important; letter-spacing: -0.005em !important;
            margin-top: 1.6rem !important;
        }}
        h3 {{
            font-size: 0.95rem !important; font-weight: 600 !important;
            color: #4B5563 !important;
        }}
        .ar-subtitle {{
            color: #6B7280; font-size: 0.92rem;
            margin: 0 0 1.5rem 0;
        }}
        .ar-section-label {{
            font-size: 0.7rem; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.08em;
            color: #6B7280; margin: 1.3rem 0 0.5rem 0;
        }}

        /* ---- Metric cards ---- */
        [data-testid="stMetric"] {{
            background: #FFFFFF;
            border: 1px solid #E5E7EB;
            border-radius: 10px;
            padding: 0.95rem 1.2rem;
            box-shadow: 0 1px 2px rgba(0,0,0,0.03);
        }}
        [data-testid="stMetricLabel"] p {{
            font-size: 0.68rem !important;
            font-weight: 600 !important;
            letter-spacing: 0.09em !important;
            text-transform: uppercase !important;
            color: #9CA3AF !important;
        }}
        [data-testid="stMetricValue"] {{
            font-size: 1.6rem !important;
            font-weight: 700 !important;
            color: #111827 !important;
            line-height: 1.2 !important;
        }}
        [data-testid="stMetricDelta"] svg {{ display: none; }}
        [data-testid="stMetricDelta"] {{
            color: #6B7280 !important;
            font-size: 0.75rem !important;
        }}

        /* ---- Dataframe ---- */
        [data-testid="stDataFrame"] {{
            border: 1px solid #E5E7EB;
            border-radius: 8px;
            overflow: hidden;
        }}

        /* ---- Buttons ---- */
        [data-testid="stButton"] > button,
        [data-testid="stFormSubmitButton"] > button {{
            background: #111827;
            color: #FFFFFF;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            font-size: 0.875rem;
            padding: 0.55rem 1.4rem;
            transition: background 0.12s;
        }}
        [data-testid="stButton"] > button:hover,
        [data-testid="stFormSubmitButton"] > button:hover {{
            background: #1F2937;
        }}
        [data-testid="stFormSubmitButton"] > button[kind="primary"] {{
            background: #E8473F;
        }}
        [data-testid="stFormSubmitButton"] > button[kind="primary"]:hover {{
            background: #CC3A33;
        }}

        /* ---- Form inputs ---- */
        [data-testid="stTextInput"] input,
        [data-testid="stSelectbox"] [data-baseweb="select"] > div {{
            border: 1px solid #D1D5DB !important;
            border-radius: 7px !important;
            font-size: 0.875rem !important;
            background: #FFFFFF !important;
        }}
        [data-testid="stTextInput"] input:focus {{
            border-color: #111827 !important;
            box-shadow: 0 0 0 2px rgba(17,24,39,0.08) !important;
        }}

        /* ---- Expander ---- */
        [data-testid="stExpander"] {{
            border: 1px solid #E5E7EB !important;
            border-radius: 8px !important;
            overflow: hidden;
        }}
        [data-testid="stExpander"] summary {{
            font-size: 0.875rem;
            font-weight: 500;
            color: #374151;
            padding: 0.75rem 1rem;
            background: #F9FAFB;
        }}
        [data-testid="stExpander"] summary:hover {{ background: #F3F4F6; }}

        /* ---- Card + empty state ---- */
        .ar-card {{
            background: #FFFFFF;
            border: 1px solid #E5E7EB;
            border-radius: 12px;
            padding: 1.25rem 1.5rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }}
        .ar-card .ar-card-title {{
            font-size: 0.7rem; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.08em;
            color: #6B7280; margin-bottom: 0.4rem;
        }}
        .ar-card .ar-card-body {{
            font-size: 0.92rem; color: #374151;
            line-height: 1.45;
        }}
        .ar-empty {{
            border: 1px dashed #D1D5DB;
            border-radius: 12px;
            padding: 2rem 1.5rem;
            text-align: center;
            background: #FAFAFB;
        }}
        .ar-empty-title {{
            font-size: 1rem; font-weight: 600;
            color: #374151; margin-bottom: 0.25rem;
        }}
        .ar-empty-body {{
            font-size: 0.85rem; color: #6B7280;
            max-width: 460px; margin: 0 auto;
        }}

        /* ---- Type / confidence badges ---- */
        .badge-company {{
            background: #DBEAFE; color: #1E40AF;
            padding: 3px 10px; border-radius: 999px;
            font-size: 0.72rem; font-weight: 600;
        }}
        .badge-person {{
            background: #EDE9FE; color: #6D28D9;
            padding: 3px 10px; border-radius: 999px;
            font-size: 0.72rem; font-weight: 600;
        }}
        .badge-neutral {{
            background: #F3F4F6; color: #374151;
            padding: 3px 10px; border-radius: 999px;
            font-size: 0.72rem; font-weight: 600;
        }}
        .conf-high {{ background: #DCFCE7; color: #166534; padding: 3px 10px; border-radius: 999px; font-size: 0.72rem; font-weight: 600; }}
        .conf-mid  {{ background: #FEF3C7; color: #92400E; padding: 3px 10px; border-radius: 999px; font-size: 0.72rem; font-weight: 600; }}
        .conf-low  {{ background: #FEE2E2; color: #991B1B; padding: 3px 10px; border-radius: 999px; font-size: 0.72rem; font-weight: 600; }}
        .conf-none {{ background: #F3F4F6; color: #6B7280; padding: 3px 10px; border-radius: 999px; font-size: 0.72rem; font-weight: 600; }}

        .ar-meta {{ color: #6B7280; font-size: 0.85rem; }}
        .ar-meta strong {{ color: #111827; font-weight: 600; }}

        /* ---- Agent stepper (Run page) ---- */
        .ar-stepper {{
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 0.5rem;
            margin: 0.5rem 0 0.7rem 0;
        }}
        .ar-step {{
            border: 1px solid #E5E7EB;
            border-radius: 10px;
            padding: 0.55rem 0.6rem;
            background: #FFFFFF;
            transition: background 0.15s, border-color 0.15s;
        }}
        .ar-step-num {{
            font-size: 0.62rem; font-weight: 600;
            color: #9CA3AF; letter-spacing: 0.08em;
        }}
        .ar-step-name {{
            font-size: 0.78rem; font-weight: 600;
            color: #111827; margin: 0.15rem 0 0.4rem 0;
            line-height: 1.25;
        }}
        .ar-step-iter {{
            font-size: 0.62rem; font-weight: 500;
            color: #9CA3AF; letter-spacing: 0.04em;
        }}
        .ar-step-state {{
            font-size: 0.72rem; font-weight: 600;
            color: #6B7280;
        }}
        .ar-step-timer {{
            font-size: 0.7rem; color: #6B7280;
            margin-top: 0.2rem;
            font-variant-numeric: tabular-nums;
        }}
        .ar-step.pending {{ background: #F9FAFB; }}
        .ar-step.running {{
            background: #FEF3C7; border-color: #F59E0B;
            box-shadow: 0 0 0 3px rgba(245,158,11,0.18);
        }}
        .ar-step.done    {{ background: #F0FDF4; border-color: #BBF7D0; }}
        .ar-step.skipped {{ background: #F3F4F6; opacity: 0.62; }}
        .ar-step.failed  {{ background: #FEF2F2; border-color: #FCA5A5; }}
        .ar-step.halted  {{ background: #FEF2F2; border-color: #F59E0B; }}
        .ar-step.running .ar-step-state {{ color: #92400E; }}
        .ar-step.done    .ar-step-state {{ color: #166534; }}
        .ar-step.failed  .ar-step-state {{ color: #991B1B; }}
        .ar-step.halted  .ar-step-state {{ color: #991B1B; }}
        .ar-step.skipped .ar-step-name  {{ text-decoration: line-through; }}
        @keyframes ar-pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.55; }} }}
        .ar-step.running .ar-step-state::before {{
            content: "● "; animation: ar-pulse 1.2s infinite;
        }}
        .ar-step.done    .ar-step-state::before {{ content: "✓ "; }}
        .ar-step.failed  .ar-step-state::before {{ content: "✕ "; }}
        .ar-step.halted  .ar-step-state::before {{ content: "⚠ "; }}
        .ar-step.skipped .ar-step-state::before {{ content: "– "; }}
        .ar-step.pending .ar-step-state::before {{ content: "○ "; }}

        .ar-stepper-foot {{
            display: flex; gap: 1.5rem;
            font-size: 0.78rem; color: #6B7280;
            margin: 0 0 1.1rem 0;
        }}
        .ar-stepper-foot strong {{
            color: #111827; font-weight: 600;
            font-variant-numeric: tabular-nums;
        }}
        </style>
        """
    )


def setup_page(title: str) -> None:
    st.set_page_config(
        page_title=f"{title} — Account Research",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_global_styles()
    _render_brand()


def _render_brand() -> None:
    with st.sidebar:
        st.html(
            """
            <div class="ar-brand">
              <div class="ar-brand-mark">AR</div>
              <div>
                <div class="ar-brand-name">Account Research</div>
                <div class="ar-brand-tag">Citation-grounded briefs</div>
              </div>
            </div>
            """
        )


def sidebar_section(label: str) -> None:
    with st.sidebar:
        st.html(f"<div class='ar-sb-section'>{label}</div>")


def page_header(title: str, subtitle: str | None = None) -> None:
    st.html(f"<h1>{title}</h1>")
    if subtitle:
        st.html(f"<p class='ar-subtitle'>{subtitle}</p>")


def section_label(text: str) -> None:
    st.html(f"<div class='ar-section-label'>{text}</div>")


def type_pill(entity_type: str) -> str:
    t = (entity_type or "").lower()
    if t == "company":
        return "<span class='badge-company'>Company</span>"
    if t == "person":
        return "<span class='badge-person'>Person</span>"
    return f"<span class='badge-neutral'>{entity_type}</span>"


def type_dataframe_label(entity_type: str) -> str:
    t = (entity_type or "").lower()
    if t == "company":
        return "Company"
    if t == "person":
        return "Person"
    return entity_type or "—"


def confidence_badge(score: float | None, is_fallback: bool = False) -> str:
    if score is None:
        return "<span class='conf-none'>—</span>"
    label = f"{score:.0f}{'*' if is_fallback else ''}"
    if score >= 80:
        cls = "conf-high"
    elif score >= 60:
        cls = "conf-mid"
    else:
        cls = "conf-low"
    return f"<span class='{cls}'>{label}</span>"


def empty_state(title: str, body: str) -> None:
    st.html(
        f"<div class='ar-empty'>"
        f"<div class='ar-empty-title'>{title}</div>"
        f"<div class='ar-empty-body'>{body}</div>"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# Agent stepper (used by pages/2_Run.py)
# ---------------------------------------------------------------------------

AGENTS: list[tuple[str, str]] = [
    ("disambiguator", "Disambig"),
    ("researcher",    "Researcher"),
    ("fact_checker",  "Fact-Check"),
    ("estimator",     "Estimator"),
    ("author",        "Author"),
    ("designer",      "Designer"),
    ("reviewer",      "Reviewer"),
]


def _fmt_mmss(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def agent_stepper_html(states: dict[str, dict]) -> str:
    """Build the stepper HTML.

    `states[agent_key]` is a dict with keys:
        state:     "pending" | "running" | "done" | "skipped" | "failed" | "halted"
        elapsed_s: float | None
        iter:      tuple[int, int] | None
    Missing entries default to pending.
    """
    cells: list[str] = []
    for i, (key, label) in enumerate(AGENTS, 1):
        s = states.get(key) or {}
        state = s.get("state", "pending")
        elapsed = s.get("elapsed_s")
        it = s.get("iter")
        iter_html = (
            f"<div class='ar-step-iter'>iter {it[0]}/{it[1]}</div>"
            if it else ""
        )
        cells.append(
            f"<div class='ar-step {state}'>"
            f"<div class='ar-step-num'>{i:02d}</div>"
            f"<div class='ar-step-name'>{label}</div>"
            f"{iter_html}"
            f"<div class='ar-step-state'>{state.upper()}</div>"
            f"<div class='ar-step-timer'>{_fmt_mmss(elapsed)}</div>"
            f"</div>"
        )
    return f"<div class='ar-stepper'>{''.join(cells)}</div>"


def stepper_footer_html(total_s: float | None, iter_pair: tuple[int, int] | None) -> str:
    parts = [f"<div>Total: <strong>{_fmt_mmss(total_s)}</strong></div>"]
    if iter_pair:
        parts.append(f"<div>Reviewer loop: <strong>iter {iter_pair[0]}/{iter_pair[1]}</strong></div>")
    return f"<div class='ar-stepper-foot'>{''.join(parts)}</div>"


# ---------------------------------------------------------------------------
# [PROGRESS] line parser + stepper state machine (used by Run + Library pages)
# ---------------------------------------------------------------------------


PROGRESS_RE = re.compile(
    r"^\[PROGRESS\] agent=(?P<agent>\w+) status=(?P<status>\w+) "
    r"ts=(?P<ts>\S+)"
    r"(?: iter=(?P<iter>\d+)/(?P<max>\d+))?"
    r"(?: info=(?P<info>\S+))?$"
)


def initial_states() -> dict[str, dict]:
    """Empty stepper state — every agent pending."""
    return {key: {"state": "pending", "elapsed_s": None, "iter": None}
            for key, _ in AGENTS}


def apply_progress(
    states: dict[str, dict],
    m: "re.Match[str]",
    now: datetime,  # noqa: ARG001 — kept for signature symmetry
) -> tuple[int, int] | None:
    """Mutate `states` from one [PROGRESS] match. Returns latest iter pair."""
    agent = m.group("agent")
    status = m.group("status")
    ts = datetime.fromisoformat(m.group("ts"))
    iter_pair: tuple[int, int] | None = None
    if m.group("iter"):
        iter_pair = (int(m.group("iter")), int(m.group("max")))

    cur = states.setdefault(
        agent, {"state": "pending", "elapsed_s": None, "iter": None}
    )
    if iter_pair:
        cur["iter"] = iter_pair

    if status == "start":
        cur["_accum_s"] = float(cur.get("elapsed_s") or 0.0)
        cur["_start_ts"] = ts
        cur["state"] = "running"
    elif status in ("done", "failed", "halted", "skipped"):
        if status == "skipped":
            cur["elapsed_s"] = cur.get("elapsed_s")
        else:
            start = cur.get("_start_ts")
            base = float(cur.get("_accum_s") or 0.0)
            if start is not None:
                cur["elapsed_s"] = base + (ts - start).total_seconds()
            elif cur.get("elapsed_s") is None:
                cur["elapsed_s"] = base
        cur["state"] = status
        cur.pop("_start_ts", None)

    return iter_pair


def tick_running(states: dict[str, dict], now: datetime) -> None:
    """Live-clock update for any agent currently running."""
    for cur in states.values():
        if cur.get("state") == "running" and cur.get("_start_ts"):
            base = float(cur.get("_accum_s") or 0.0)
            cur["elapsed_s"] = base + (now - cur["_start_ts"]).total_seconds()


def total_elapsed(states: dict[str, dict]) -> float | None:
    total = 0.0
    seen = False
    for cur in states.values():
        v = cur.get("elapsed_s")
        if v is not None:
            total += float(v)
            seen = True
    return total if seen else None


def strip_internal_keys(states: dict[str, dict]) -> dict[str, dict]:
    """Drop `_start_ts`/`_accum_s` from each agent state (UI-public view)."""
    return {
        k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
        for k, v in states.items()
    }
