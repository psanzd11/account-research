"""Streamlit UI for the account-research-agents pipeline.

Run with:
    streamlit run Home.py

Lives at repo root; pages/ defines Library, Run, Costs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from account_research.db import SessionLocal, init_db
from account_research.ledger import EntityRow, EstimateRow, EvidenceRow, PipelineRunRow
from utils.ui import empty_state, page_header, section_label, setup_page


setup_page("Home")
init_db()


@st.cache_data(ttl=30)
def _summary_stats():
    with SessionLocal() as s:
        n_entities = s.query(EntityRow).count()
        n_items = s.query(EvidenceRow).count()
        n_verified = s.query(EvidenceRow).filter(
            EvidenceRow.verification_status == "verified"
        ).count()
        n_estimates = s.query(EstimateRow).count()
        n_runs = s.query(PipelineRunRow).count()
        n_runs_completed = s.query(PipelineRunRow).filter(
            PipelineRunRow.status == "completed"
        ).count()
    pdf_dir = REPO_ROOT / "outputs" / "pdfs"
    n_pdfs = len(list(pdf_dir.glob("*.pdf"))) if pdf_dir.exists() else 0
    return dict(
        entities=n_entities,
        items=n_items,
        verified=n_verified,
        estimates=n_estimates,
        runs=n_runs,
        runs_completed=n_runs_completed,
        pdfs=n_pdfs,
    )


page_header(
    "Account Research",
    "Citation-grounded multi-agent pipeline that produces 4-page PDF briefs.",
)

stats = _summary_stats()
verified_pct = (stats["verified"] / stats["items"] * 100) if stats["items"] else 0

c1, c2, c3, c4 = st.columns(4)
c1.metric("Entities", f"{stats['entities']:,}")
c2.metric(
    "Ledger items",
    f"{stats['items']:,}",
    delta=f"{stats['verified']:,} verified ({verified_pct:.0f}%)",
)
c3.metric("Estimates", f"{stats['estimates']:,}")
c4.metric("PDFs generated", f"{stats['pdfs']:,}")

section_label("What is this")
st.html(
    "<div class='ar-card'><div class='ar-card-body'>"
    "A pipeline of seven specialised AI agents (Disambiguator, Researcher, "
    "Fact-Checker, Estimator, Author, Designer, Reviewer) that builds a "
    "citation-grounded research brief on any person or company. Every claim "
    "in the final PDF traces back to a sourced quote in the evidence ledger. "
    "Estimates come from named Python recipes — never improvised."
    "</div></div>"
)

section_label("Where to go")
n1, n2, n3 = st.columns(3, gap="medium")
with n1:
    st.html(
        "<div class='ar-card'>"
        "<div class='ar-card-title'>Library</div>"
        "<div class='ar-card-body' style='margin-bottom:0.8rem;'>"
        "Browse every entity already researched. Preview the PDF, inspect "
        "the evidence ledger, audit the LLM trace per run."
        "</div></div>"
    )
    st.page_link("pages/1_Library.py", label="Open Library →")
with n2:
    st.html(
        "<div class='ar-card'>"
        "<div class='ar-card-title'>Run</div>"
        "<div class='ar-card-body' style='margin-bottom:0.8rem;'>"
        "Launch the pipeline on a fresh query. Pick entity type and geo "
        "hint, watch the agents work, download the PDF on approval."
        "</div></div>"
    )
    st.page_link("pages/2_Run.py", label="Open Run →")
with n3:
    st.html(
        "<div class='ar-card'>"
        "<div class='ar-card-title'>Costs</div>"
        "<div class='ar-card-body' style='margin-bottom:0.8rem;'>"
        "Aggregate every LLM call — per-day spend, per-agent and per-model "
        "breakdowns. Useful to size future runs."
        "</div></div>"
    )
    st.page_link("pages/3_Costs.py", label="Open Costs →")

with st.expander("Pipeline architecture"):
    st.markdown(
        """
        ```
        [1 Disambiguator]   web_search to resolve query → entity (halts on ambiguous)
        [2 Researcher]      web_search + web_fetch to build evidence ledger
        [3 Fact-Checker]    re-fetches every source, substring-matches raw_quote
                            (Haiku web_fetch fallback for JS-rendered pages)
        [4 Estimator]       pure Python recipes (consulting/saas/net_worth);
                            returns InsufficientSignals when recipe doesn't fit
        [5 Author]          Opus 4.7 drafts BriefData with citation discipline
        [6 Designer]        renders 4-page PDF; omits empty sections, never pads
        [7 Reviewer]        Opus 4.7 + pdfplumber checks every claim traces
                            back to evidence_id / method_id; demands revisions
                            (max 3 iterations, then escalates to human review)
        ```
        """
    )

if stats["entities"] == 0:
    empty_state(
        "Nothing in the library yet",
        "Head over to Run and launch your first brief — it takes 3–5 minutes "
        "and produces a 4-page PDF with every claim traced to a source.",
    )
