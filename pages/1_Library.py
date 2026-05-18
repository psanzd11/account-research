"""Library page — browse entities, preview PDFs, inspect ledger + briefs."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from account_research.db import SessionLocal
from account_research.ledger import (
    EntityRow, EstimateRow, EvidenceRow, PipelineRunRow,
    get_evidence_for_entity,
)
from account_research.quality import (
    citation_backing,
    confidence_score,
    ledger_only_score,
    sections_rendered,
    tier1_share_among_cited,
)
from account_research.schemas.brief import BriefData
from account_research.schemas.evidence import VerifiedEvidenceItem
from utils.ui import (
    PROGRESS_RE,
    agent_stepper_html,
    apply_progress,
    empty_state,
    initial_states,
    page_header,
    section_label,
    setup_page,
    sidebar_section,
    stepper_footer_html,
    strip_internal_keys,
    tick_running,
    total_elapsed,
    type_dataframe_label,
    type_pill,
)


setup_page("Library")

PDF_DIR = REPO_ROOT / "outputs" / "pdfs"
BRIEFS_DIR = REPO_ROOT / "outputs" / "briefs"


@st.cache_data(ttl=15)
def load_entities():
    rows = []
    with SessionLocal() as s:
        for e in s.query(EntityRow).order_by(EntityRow.created_at.desc()).all():
            items = s.query(EvidenceRow).filter(EvidenceRow.entity_id == e.id).all()
            n = len(items)
            verified = sum(1 for r in items if r.verification_status == "verified")
            unverif = sum(1 for r in items if r.verification_status == "unverifiable")
            dead = sum(1 for r in items if r.verification_status == "source_dead")
            ests = s.query(EstimateRow).filter(EstimateRow.entity_id == e.id).all()
            est_str = ", ".join(f"{ex.value_range} ({ex.confidence})" for ex in ests) if ests else "—"
            # Sourcing rate = verified / (verified + unverifiable). Excludes
            # source_dead from the denominator: dead URLs are link rot
            # (the Fact-Checker couldn't even re-fetch them), not a
            # Researcher quality signal. Including them in the denominator
            # punishes refine runs that grow the ledger.
            checkable = verified + unverif
            sourcing_rate = (verified / checkable) if checkable else 0.0
            pdfs = list(PDF_DIR.glob(f"*{e.id}*.pdf"))
            pdf_path = str(pdfs[0]) if pdfs else None
            brief_path = BRIEFS_DIR / f"{e.id}.json"

            brief_metrics: dict[str, float | None | bool] = {
                "citation_backing": None,
                "tier1_share_cited": None,
                "sections_pop": None,
                "sections_total": None,
                "confidence_score": None,
                "is_fallback_confidence": False,
            }
            full_score_loaded = False
            if brief_path.exists():
                try:
                    brief = BriefData.model_validate_json(
                        brief_path.read_text(encoding="utf-8")
                    )
                    ledger = [
                        ev for ev in get_evidence_for_entity(s, e.id)
                        if isinstance(ev, VerifiedEvidenceItem)
                    ]
                    pop, total = sections_rendered(brief)
                    brief_metrics = {
                        "citation_backing": citation_backing(brief, ledger),
                        "tier1_share_cited": tier1_share_among_cited(brief, ledger),
                        "sections_pop": pop,
                        "sections_total": total,
                        "confidence_score": confidence_score(brief, ledger),
                        "is_fallback_confidence": False,
                    }
                    full_score_loaded = True
                except Exception:
                    pass
            if not full_score_loaded:
                fallback = ledger_only_score(items)
                if fallback is not None:
                    brief_metrics["confidence_score"] = fallback
                    brief_metrics["is_fallback_confidence"] = True

            rows.append({
                "id": str(e.id),
                "name": e.name,
                "type": e.type,
                "items": n,
                "verified": verified,
                "unverif": unverif,
                "dead": dead,
                "rate": sourcing_rate,
                "sourcing_rate": sourcing_rate,
                "estimate": est_str,
                "created_at": e.created_at,
                "pdf": pdf_path,
                "brief": str(brief_path) if brief_path.exists() else None,
                "url": e.primary_url or "",
                **brief_metrics,
            })
    return rows


def _render_pdf_iframe(path: str, height: int = 850) -> str:
    data = Path(path).read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return (
        f'<iframe src="data:application/pdf;base64,{b64}" '
        f'width="100%" height="{height}" '
        f'style="border:1px solid #E5E7EB; border-radius:12px;"></iframe>'
    )


def _load_brief_json(brief_path: str) -> dict | None:
    try:
        return json.loads(Path(brief_path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _entity_runs(entity_id: UUID) -> list[dict]:
    with SessionLocal() as s:
        runs = (
            s.query(PipelineRunRow)
            .filter(PipelineRunRow.entity_id == entity_id)
            .order_by(PipelineRunRow.started_at.desc())
            .all()
        )
        return [
            {
                "id": str(r.id),
                "query": r.query,
                "status": r.status,
                "started_at": r.started_at,
                "completed_at": r.completed_at,
                "iterations": r.iterations,
            }
            for r in runs
        ]


def _latest_run_status(entity_id: UUID) -> str | None:
    """Status string of the most recent pipeline_run for this entity.

    Returns None when no run exists with the entity_id back-filled.
    Used to surface 'completed_author_fallback' banners.
    """
    with SessionLocal() as s:
        r = (
            s.query(PipelineRunRow)
            .filter(PipelineRunRow.entity_id == entity_id)
            .order_by(PipelineRunRow.started_at.desc())
            .first()
        )
    return r.status if r else None


def _entity_evidence(entity_id: UUID) -> pd.DataFrame:
    with SessionLocal() as s:
        rows = (
            s.query(EvidenceRow)
            .filter(EvidenceRow.entity_id == entity_id)
            .order_by(EvidenceRow.fetched_at.asc())
            .all()
        )
        return pd.DataFrame([
            {
                "id": str(r.id)[:8],
                "category": r.category,
                "claim": r.claim,
                "raw_quote": r.raw_quote[:120] + ("…" if len(r.raw_quote) > 120 else ""),
                "confidence": r.confidence,
                "verified": r.verification_status or "—",
                "source_type": r.source_type,
                "source_url": r.source_url,
            }
            for r in rows
        ])


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

page_header("Library", "Every entity ever researched. Click a row to inspect.")

entities = load_entities()
if not entities:
    empty_state(
        "No entities yet",
        "Head over to Run and launch your first brief — once it finishes, "
        "the entity will appear here with its PDF, ledger and run history.",
    )
    st.stop()

df = pd.DataFrame(entities)

with st.sidebar:
    st.markdown("---")
    sidebar_section("Filters")
    types = sorted(df["type"].unique().tolist())
    type_filter = st.multiselect("Entity type", types, default=types)
    name_query = st.text_input("Name contains", "")
    min_items = st.slider("Min ledger items", 0, int(df["items"].max() or 1), 0)
    has_pdf_only = st.checkbox("Only entities with a PDF", value=False)

filtered = df[df["type"].isin(type_filter)]
if name_query:
    filtered = filtered[filtered["name"].str.contains(name_query, case=False, na=False)]
filtered = filtered[filtered["items"] >= min_items]
if has_pdf_only:
    filtered = filtered[filtered["pdf"].notna()]

n_companies = int((filtered["type"] == "company").sum())
n_persons = int((filtered["type"] == "person").sum())
scores_present = filtered["confidence_score"].dropna()
avg_conf = float(scores_present.mean()) if not scores_present.empty else None

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total entities", f"{len(filtered):,}", delta=f"of {len(df):,} loaded")
k2.metric("Companies", f"{n_companies:,}")
k3.metric("Persons", f"{n_persons:,}")
k4.metric(
    "Avg. confidence",
    f"{avg_conf:.0f}" if avg_conf is not None else "—",
    delta="across filtered set" if avg_conf is not None else "no scores yet",
)

st.html(
    f"<div class='ar-meta' style='margin-top:1.4rem;'>"
    f"<strong>{len(filtered)}</strong> of {len(df)} entities shown</div>"
)


def _conf_mode(row) -> str:
    v = row["confidence_score"]
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return "Ledger*" if bool(row.get("is_fallback_confidence")) else "Full"


def _verified_str(row) -> str:
    items = int(row["items"])
    if items == 0:
        return "—"
    return f"{int(row['verified'])}/{items}"


display_df = filtered.copy()
display_df["type_label"] = display_df["type"].map(type_dataframe_label)
display_df["verified_str"] = display_df.apply(_verified_str, axis=1)
display_df["sourcing_rate_pct"] = (display_df["sourcing_rate"] * 100).round(0)
display_df["conf_value"] = display_df["confidence_score"].fillna(0).astype(float)
display_df["conf_mode"] = display_df.apply(_conf_mode, axis=1)

display_df = display_df[[
    "name", "type_label", "verified_str", "sourcing_rate_pct",
    "conf_value", "conf_mode", "estimate", "created_at",
]]

event = st.dataframe(
    display_df,
    width="stretch",
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "name": st.column_config.TextColumn("Name", width="large"),
        "type_label": st.column_config.TextColumn("Type", width="small"),
        "verified_str": st.column_config.TextColumn(
            "Verified / Items", width="small",
            help="Verified ledger items over total items in the ledger.",
        ),
        "sourcing_rate_pct": st.column_config.NumberColumn(
            "Sourcing Rate", format="%.0f%%", width="medium",
            help="Of the items the Fact-Checker could evaluate (verified + "
                 "unverifiable), share that came back verified. Excludes "
                 "source_dead (URL rot) from the denominator — those count "
                 "against link freshness, not Researcher quality.",
        ),
        "conf_value": st.column_config.ProgressColumn(
            "Confidence", format="%.0f", min_value=0, max_value=100, width="medium",
            help="Composite 0-100 over the brief. See Mode column for the source.",
        ),
        "conf_mode": st.column_config.TextColumn(
            "Mode", width="small",
            help="Full = brief-based score. Ledger* = fallback computed from "
                 "the ledger alone (60% verified + 30% Tier-1 + 10% volume).",
        ),
        "estimate": st.column_config.TextColumn(
            "Estimate", width="large",
            help="Estimate range and confidence from the Estimator recipes.",
        ),
        "created_at": st.column_config.DatetimeColumn(
            "Created", format="MMM D, YYYY", width="medium",
        ),
    },
)

selected_rows = event.selection.rows
if not selected_rows:
    st.markdown("##")
    empty_state(
        "Select an entity above",
        "Pick any row to inspect its PDF, evidence ledger, brief JSON, run "
        "history and metadata in a tabbed detail panel.",
    )
    st.stop()

selected = filtered.iloc[selected_rows[0]].to_dict()
entity_id = UUID(selected["id"])

hdr_col, btn_col = st.columns([6, 1])
with hdr_col:
    st.html(
        f"<h2 style='margin-bottom:0.2rem;'>{selected['name']}</h2>"
        f"<div style='margin-bottom:1rem;'>{type_pill(selected['type'])}</div>"
    )
with btn_col:
    st.markdown("##")
    refine_clicked = st.button(
        "Refine brief",
        key=f"refine_{entity_id}",
        help="Run a supplemental research round focused on this brief's "
             "weaknesses (coverage gaps, low Tier-1 share, dead links). "
             "Reuses the existing ledger — does NOT re-run the full pipeline.",
        width="stretch",
    )

# Surface the Author-fallback state when the most recent run hit it.
# The brief on disk is the PRIOR draft (or a skeleton); the ledger may
# have grown without a new Author pass having interpreted it.
_latest_status = _latest_run_status(entity_id)
if _latest_status == "completed_author_fallback":
    st.warning(
        "**Last run fell back to the prior brief — Author crashed.** "
        "The ledger may have grown but the PDF content was not rewritten. "
        "Inspect the log under the most recent run in the Pipeline runs tab, "
        "then click *Refine brief* again to retry the Author.",
        icon=":material/warning:",
    )


def _stream_refine(entity_id: UUID, focus: str | None) -> None:
    """Spawn `python -m account_research.cli refine <id>` and stream its
    stdout into an inline stepper + log box (same UX as the Run page)."""
    cmd: list[str] = [
        sys.executable, "-m", "account_research.cli", "refine", str(entity_id), "-v",
    ]
    if focus:
        cmd.extend(["--focus", focus])

    st.markdown("##")
    st.markdown("**Refine command**")
    st.code(" ".join(c if " " not in c else f'"{c}"' for c in cmd), language="bash")

    stepper_box = st.empty()
    stepper_foot = st.empty()
    log_lines: list[str] = []
    log_box = st.empty()

    states = initial_states()
    latest_iter: tuple[int, int] | None = None
    stepper_box.html(agent_stepper_html(strip_internal_keys(states)))
    stepper_foot.html(stepper_footer_html(None, None))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    with st.status(f"Refining brief for entity {entity_id}…", expanded=True) as status:
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, env=env,
            )
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip()
                if not line:
                    continue
                now = datetime.now(timezone.utc)
                m = PROGRESS_RE.match(line)
                if m:
                    new_iter = apply_progress(states, m, now)
                    if new_iter is not None:
                        latest_iter = new_iter
                else:
                    log_lines.append(line)
                    log_box.code("\n".join(log_lines[-80:]), language="text")
                tick_running(states, now)
                stepper_box.html(agent_stepper_html(strip_internal_keys(states)))
                in_loop = any(
                    states.get(k, {}).get("state") == "running"
                    for k in ("author", "designer", "reviewer")
                )
                stepper_foot.html(stepper_footer_html(
                    total_elapsed(states), latest_iter if in_loop else None,
                ))
            proc.wait()
            exit_code = proc.returncode
        except Exception as exc:  # noqa: BLE001
            status.update(label=f"Crashed: {exc}", state="error")
            st.exception(exc)
            return

        tick_running(states, datetime.now(timezone.utc))
        stepper_box.html(agent_stepper_html(strip_internal_keys(states)))
        stepper_foot.html(stepper_footer_html(total_elapsed(states), None))

        if exit_code == 0:
            status.update(label="Refine completed", state="complete")
        elif exit_code == 6:
            status.update(label="Refine aborted: empty ledger", state="error")
        else:
            status.update(label=f"Refine failed (exit {exit_code})", state="error")


if refine_clicked:
    st.session_state[f"refine_open_{entity_id}"] = True

if st.session_state.get(f"refine_open_{entity_id}"):
    with st.container():
        section_label("Refine — supplemental research + re-author")
        c_focus, c_go = st.columns([3, 1])
        focus = c_focus.selectbox(
            "Focus category (optional)",
            ["(auto-detect)", "company_facts", "financial", "leadership",
             "products", "clients", "geography"],
            help="Leave on auto to let the analyzer pick weaknesses. Override "
                 "when you know which area needs strengthening.",
            key=f"refine_focus_{entity_id}",
        )
        c_go.markdown("##")
        run_refine = c_go.button(
            "Run refine", type="primary", key=f"refine_run_{entity_id}",
            width="stretch",
        )
        if run_refine:
            _stream_refine(
                entity_id,
                focus=None if focus == "(auto-detect)" else focus,
            )
            # Force a fresh load_entities() pull next time.
            load_entities.clear()
            st.info(
                "Library data cached — reselect the entity row above to see "
                "the refreshed confidence score.",
            )


def _fmt_pct(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value:.0f}%"


def _fmt_score(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value:.0f}"


section_label("Sourcing — quality of the Researcher's evidence batch")
cm1, cm2, cm3, cm4 = st.columns(4)
cm1.metric("Ledger items", f"{int(selected['items']):,}")
cm2.metric("Verified", f"{int(selected['verified']):,}")
cm3.metric(
    "Sourcing rate",
    f"{int(selected['sourcing_rate']*100)}%",
    help="Of the items the Fact-Checker could evaluate (verified + "
         "unverifiable), share that came back verified. Excludes "
         "source_dead (URL rot) from the denominator so a refine "
         "doesn't tank this number just by discovering more 404'd links.",
)
cm4.metric("Estimate", selected["estimate"], help=selected["estimate"])

section_label("Brief — quality of what shipped in the PDF")
bm1, bm2, bm3, bm4 = st.columns(4)
bm1.metric(
    "Citation backing",
    _fmt_pct(selected.get("citation_backing")),
    help="% of evidence_ids cited in the brief whose status is 'verified'.",
)
bm2.metric(
    "Tier-1 share (cited)",
    _fmt_pct(selected.get("tier1_share_cited")),
    help="% of cited evidence whose source is Tier-1 (official site, SEC, "
         "gov registry, major business press).",
)
pop = selected.get("sections_pop")
tot = selected.get("sections_total")
bm3.metric(
    "Sections rendered",
    f"{int(pop)}/{int(tot)}" if pop is not None and tot is not None else "—",
    help="Number of PDF sections populated (out of 17 possible).",
)
_score_val = selected.get("confidence_score")
_score_is_fallback = bool(selected.get("is_fallback_confidence"))
_score_str = _fmt_score(_score_val)
if _score_str != "—" and _score_is_fallback:
    _score_str = f"{_score_str}*"
bm4.metric(
    "Confidence score",
    _score_str,
    help=(
        "Composite 0-100: 50% citation backing + 25% sections rendered + "
        "15% Tier-1 share + 10% estimate caveat present. A trailing `*` "
        "means a ledger-only fallback."
    ),
)

with st.expander("Sourcing diagnostics (Researcher detail)"):
    sd1, sd2, sd3 = st.columns(3)
    sd1.metric("Verified", selected["verified"])
    sd2.metric("Unverifiable", selected["unverif"])
    sd3.metric("Source dead", selected["dead"])
    st.caption(
        "These come from the full ledger persisted in the DB, before the "
        "Author filter. Author only sees the verified set plus Tier-1/HIGH "
        "fallbacks; unverifiable & source_dead items never reach the PDF."
    )


def _truthy_path(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return None
    s = str(value).strip()
    return s or None


if _truthy_path(selected["url"]):
    st.html(
        f"<div class='ar-meta' style='margin-top:0.8rem;'><strong>Primary URL:</strong> "
        f"<a href='{selected['url']}' target='_blank'>{selected['url']}</a></div>"
    )

t_pdf, t_brief, t_evidence, t_runs, t_meta = st.tabs(
    ["PDF", "Brief JSON", "Evidence ledger", "Pipeline runs", "Entity meta"]
)

with t_pdf:
    _pdf = _truthy_path(selected["pdf"])
    if _pdf and Path(_pdf).exists():
        c_dl, _ = st.columns([1, 6])
        with c_dl:
            with open(_pdf, "rb") as f:
                st.download_button(
                    "Download PDF",
                    f.read(),
                    file_name=Path(_pdf).name,
                    mime="application/pdf",
                )
        st.html(_render_pdf_iframe(_pdf))
    else:
        empty_state(
            "No PDF rendered yet",
            "Run `author` then `design` on this entity, or use the Run page "
            "to redo the full pipeline.",
        )

with t_brief:
    _brief = _truthy_path(selected["brief"])
    if _brief and Path(_brief).exists():
        data = _load_brief_json(_brief)
        if data is not None:
            st.json(data, expanded=False)
        else:
            st.error("Brief JSON failed to parse.")
    else:
        empty_state(
            "No BriefData JSON saved yet",
            "The Author has not produced a brief for this entity. Run "
            "`author <id>` to materialize one.",
        )

with t_evidence:
    ev_df = _entity_evidence(entity_id)
    if ev_df.empty:
        empty_state("No evidence items", "The Researcher hasn't produced any ledger items for this entity.")
    else:
        st.html(
            f"<div class='ar-meta'><strong>{len(ev_df)}</strong> items</div>"
        )
        st.dataframe(
            ev_df,
            width="stretch",
            hide_index=True,
            column_config={
                "claim": st.column_config.TextColumn("Claim", width="large"),
                "raw_quote": st.column_config.TextColumn("Raw quote (truncated)", width="large"),
                "source_url": st.column_config.LinkColumn("URL", width="small"),
            },
        )

with t_runs:
    runs = _entity_runs(entity_id)
    if not runs:
        empty_state("No pipeline runs recorded", "This entity has no run history.")
    else:
        st.dataframe(pd.DataFrame(runs), width="stretch", hide_index=True)

with t_meta:
    st.code(
        f"entity_id: {selected['id']}\n"
        f"type:      {selected['type']}\n"
        f"name:      {selected['name']}\n"
        f"created:   {selected['created_at']}\n"
        f"url:       {selected['url']}",
        language="text",
    )
