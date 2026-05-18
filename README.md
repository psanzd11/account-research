# Account Research Agents

Multi-agent pipeline that generates citation-grounded visual PDF research briefs on persons or companies.

## What problem this solves

Single-LLM brief generation hallucinates: fabricates financial figures, pads layouts with extrapolated data, presents inferences as facts. This pipeline separates retrieval, estimation, drafting, and rendering across specialised agents — and requires every claim to trace to a sourced quote in a verifiable ledger.

## Setup (one-off)

```bash
cd account-research-agents
pip install -r requirements.txt
cp .env.example .env
# edit .env and fill in ANTHROPIC_API_KEY=sk-ant-...
```

## How to run it — the UI (recommended)

```bash
streamlit run app.py
```

Opens `http://localhost:8501` in your browser. Three pages:

- **Library** — every entity already researched. Click a row to preview the PDF, inspect the ledger, audit the BriefData JSON, browse pipeline runs.
- **Run** — form to launch a fresh brief (query, entity-type, geo, revision iterations). Streams agent logs live; produces a download link to the final PDF.
- **Costs** — aggregates every Anthropic call ever made. Per-day chart, per-agent and per-model breakdowns, raw call table.

No login, no deploy — purely local, talks straight to the SQLite ledger.

## How to run it — the CLI

The UI is a thin shell over six subcommands. You can drive any of them directly:

```bash
# Full pipeline end-to-end (Disambig → Researcher → FactCheck → Estimator → Author → Designer → Reviewer)
python -m account_research.cli run "Marco Galperin" --entity-type person --geo "Argentina"

# Halt-on-ambiguous resume: pick a candidate UUID from outputs/candidates/<slug>.json
python -m account_research.cli run "BWPM" --candidate <uuid>

# Re-run a specific stage on an existing entity (saves tokens):
python -m account_research.cli fact-check <entity_id>
python -m account_research.cli estimate   <entity_id>
python -m account_research.cli author     <entity_id>
python -m account_research.cli design     <entity_id>
python -m account_research.cli review     <entity_id>
```

Output lands in `outputs/`:

```
outputs/
├── pdfs/<Name>_<uuid>.pdf           the final 4-page brief
├── briefs/<uuid>.json               the Author's structured BriefData
├── ledger.db                        SQLite — all entities, evidence, estimates, runs
├── llm_trace/<date>.jsonl           every Anthropic call (tokens + latency)
├── web_cache/                       httpx + Anthropic web_fetch cache
└── candidates/<slug>.json           Disambiguator candidates when ambiguous
```

## Pipeline at a glance

```
1. Disambiguator  →  confirm entity (halts on ambiguous; no fallback guess)
2. Researcher     →  build evidence ledger via web_search + web_fetch
3. Fact-Checker   →  re-fetch every source, substring-match raw_quote
                     (Haiku web_fetch fallback for JS-rendered pages)
4. Estimator      →  financial estimates via Python recipes
                     (consulting/saas/net_worth); never improvised
5. Author         →  Opus 4.7 drafts BriefData; every text-bearing field
                     cites ≥1 evidence_id (or method_id for estimates)
6. Designer       →  renders 4-page PDF; omits empty sections, never pads
7. Reviewer       →  Opus 4.7 + pdfplumber audits trace; revision loop
                     up to 3 iterations, then escalates
```

## Read first

- `SPEC.md` — full architecture, agent contracts, schemas, sprint plan.
- `CLAUDE.md` — conventions and anti-patterns (read before contributing).
- `templates/pipeline_diagram.png` — visual of the pipeline.

## Status

Implementation complete (Sprints 1-5 + hardening). 78 unit + 3 integration tests
green. Validated end-to-end against the regression set (BW Project Management,
Guillermo Jaime Calderón, Stripe, Marcos Galperin, Alexandra Plasencia,
Mercado Libre).
