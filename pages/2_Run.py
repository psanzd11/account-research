"""Run page — launch the pipeline on a fresh query, stream logs live."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from account_research.db import SessionLocal
from account_research.ledger import EntityRow, PipelineRunRow
from utils.ui import (
    PROGRESS_RE,
    agent_stepper_html,
    apply_progress,
    empty_state,
    initial_states,
    page_header,
    section_label,
    setup_page,
    stepper_footer_html,
    strip_internal_keys,
    tick_running,
    total_elapsed,
)


setup_page("Run")
page_header(
    "Run a new brief",
    "Resolves the query → builds an evidence ledger → fact-checks → estimates → "
    "drafts → renders → reviews. Takes 3–5 min and costs roughly $5–15 in API.",
)

if not os.environ.get("ANTHROPIC_API_KEY"):
    st.error(
        "ANTHROPIC_API_KEY not set. Add it to `.env` at the repo root "
        "before launching streamlit (`ANTHROPIC_API_KEY=sk-ant-...`)."
    )
    st.stop()


# ---------------------------------------------------------------------------
# Form
# ---------------------------------------------------------------------------

with st.form("run_form"):
    section_label("Query")
    c1, c2 = st.columns([3, 1])
    query = c1.text_input(
        "Query",
        placeholder="e.g. 'Marcos Galperin Mercado Libre' or 'Stripe'",
        label_visibility="collapsed",
    )
    entity_type = c2.selectbox(
        "Entity type",
        ["unknown", "person", "company"],
        help="`unknown` lets the Disambiguator decide; pick `person`/`company` "
             "to constrain the search when you already know.",
    )

    section_label("Hints")
    c3, c4 = st.columns([3, 1])
    geo = c3.text_input(
        "Geography hint (optional)",
        placeholder="e.g. 'Argentina', 'Dominican Republic'",
        label_visibility="collapsed",
    )
    max_iterations = c4.slider(
        "Max revision iterations",
        1, 3, 3,
        help="How many times the Reviewer can demand a redraft before "
             "escalating to human review.",
    )

    with st.expander("Advanced options"):
        st.caption(
            "Skip stages to cut cost or speed up iteration. Each skip degrades "
            "the brief quality in a specific way — see the tooltips."
        )
        s1, s2, s3 = st.columns(3)
        skip_reviewer = s1.checkbox(
            "Skip Reviewer (single-shot)",
            help="No revision loop. Cuts cost by ~40%.",
        )
        skip_estimator = s2.checkbox(
            "Skip Estimator",
            help="No financial estimates. Badge renders INSUFFICIENT DATA.",
        )
        skip_fact_check = s3.checkbox(
            "Skip Fact-Checker",
            help="Use raw evidence without re-verification. Risky.",
        )

    st.markdown("##")
    submitted = st.form_submit_button(
        "Launch pipeline", type="primary", width="stretch",
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _build_cmd() -> list[str]:
    cmd = [sys.executable, "-m", "account_research.cli", "run", query, "-v",
           "--entity-type", entity_type,
           "--max-iterations", str(max_iterations)]
    if geo:
        cmd.extend(["--geo", geo])
    if skip_reviewer:
        cmd.append("--skip-reviewer")
    if skip_estimator:
        cmd.append("--skip-estimator")
    if skip_fact_check:
        cmd.append("--skip-fact-check")
    return cmd


def _newest_run_after(t: datetime) -> tuple[PipelineRunRow, EntityRow] | None:
    with SessionLocal() as s:
        run = (
            s.query(PipelineRunRow)
            .filter(PipelineRunRow.started_at >= t)
            .order_by(PipelineRunRow.started_at.desc())
            .first()
        )
        if run is None:
            return None
        ent = s.get(EntityRow, run.entity_id) if run.entity_id else None
        return (
            type("R", (), dict(
                id=run.id, query=run.query, status=run.status,
                started_at=run.started_at, completed_at=run.completed_at,
                iterations=run.iterations, final_pdf_path=run.final_pdf_path,
            ))(),
            type("E", (), dict(
                id=ent.id if ent else None,
                name=ent.name if ent else "(unknown)",
                type=ent.type if ent else "",
            ))() if ent else None,
        )


if not submitted:
    st.markdown("##")
    empty_state(
        "Ready to launch",
        "Fill the query above and press the launch button. The pipeline will "
        "stream its logs here, and the final PDF will be downloadable when "
        "the Reviewer approves.",
    )
    st.stop()

if not query.strip():
    st.warning("Enter a query first.")
    st.stop()

cmd = _build_cmd()
start_t = datetime.now(timezone.utc)

st.markdown("##")
st.markdown("**Command**")
st.code(" ".join(f'"{c}"' if " " in c else c for c in cmd), language="bash")

stepper_box = st.empty()
stepper_foot = st.empty()
log_lines: list[str] = []
log_box = st.empty()

states = initial_states()
latest_iter: tuple[int, int] | None = None

# Initial render so the user sees the 7 pending pills immediately.
stepper_box.html(agent_stepper_html(strip_internal_keys(states)))
stepper_foot.html(stepper_footer_html(None, None))

with st.status(f"Running pipeline on '{query}'...", expanded=True) as status:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
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

            # Tick the running clock and re-render on every line so the
            # cronometers advance with the log cadence.
            tick_running(states, now)
            stepper_box.html(agent_stepper_html(strip_internal_keys(states)))
            # Footer iter only shown while any of author/designer/reviewer is active.
            in_loop = any(
                states.get(k, {}).get("state") == "running"
                for k in ("author", "designer", "reviewer")
            )
            stepper_foot.html(
                stepper_footer_html(
                    total_elapsed(states),
                    latest_iter if in_loop else None,
                )
            )
        proc.wait()
        exit_code = proc.returncode
    except Exception as exc:
        status.update(label=f"Crashed: {exc}", state="error")
        st.exception(exc)
        st.stop()

    # Final tick + render so the stepper reflects the terminal state.
    tick_running(states, datetime.now(timezone.utc))
    stepper_box.html(agent_stepper_html(strip_internal_keys(states)))
    stepper_foot.html(stepper_footer_html(total_elapsed(states), None))

    if exit_code == 0:
        status.update(label=f"Completed (exit {exit_code})", state="complete")
    elif exit_code == 3:
        status.update(label="Halted on ambiguous (need --candidate)", state="error")
    elif exit_code == 4:
        status.update(label="Not found", state="error")
    else:
        status.update(label=f"Failed (exit {exit_code})", state="error")

st.divider()

result = _newest_run_after(start_t)
if result is None:
    st.warning("No pipeline_run record was inserted — check the logs above.")
    st.stop()

run, ent = result
cols = st.columns(4)
cols[0].metric("Run status", run.status)
cols[1].metric("Iterations", run.iterations)
if run.started_at and run.completed_at:
    dur = (run.completed_at - run.started_at).total_seconds()
    cols[2].metric("Duration", f"{dur:.0f}s")
cols[3].metric("PDF", "yes" if run.final_pdf_path else "no")

if ent and ent.id:
    st.success(f"Entity: **{ent.name}** ({ent.type})")
    st.code(f"entity_id: {ent.id}", language="text")
    st.page_link("pages/1_Library.py", label="Open in Library →")
    if run.final_pdf_path and Path(run.final_pdf_path).exists():
        with open(run.final_pdf_path, "rb") as f:
            st.download_button(
                "Download the PDF",
                f.read(),
                file_name=Path(run.final_pdf_path).name,
                mime="application/pdf",
            )
else:
    st.info("Run completed without resolving an entity.")
