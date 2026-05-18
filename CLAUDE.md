# CLAUDE.md — Project Conventions

This file tells Claude Code how to work in this repo. Read it before making changes.

## Project goal
Multi-agent pipeline that generates a citation-grounded PDF research brief on a person or company. Full design in `SPEC.md`.

## Non-negotiable rules

These exist because they were violated in v0 (manual generation) and produced hallucinated briefs. Each rule is enforced by a specific mechanism in the pipeline; do not bypass them.

1. **No claim renders without a citation.** Every fact in the final PDF must trace to an `evidence_id` in the ledger, or an `estimate.method_id` from the Methodology Library. If you find yourself writing prose without a source, stop.

2. **No padding for layout completeness.** The PDF template has slots for stat blocks, chips, cards, etc. If the ledger doesn't support filling all of them, render fewer. Empty sections render as "Insufficient public data" or are omitted entirely. Never extrapolate to fill space.

3. **Estimates are never bare numbers.** A financial badge or stat block with an estimate must carry: `value_range` (never a point), `confidence`, `method_id`, and `caveat_text` rendered next to it.

4. **Recipes, not improvisation.** Estimates come from named YAML recipes in `methodology/`. To add a new estimation type, add a new recipe file. Don't compute estimates inline in agent code.

5. **`raw_quote` is mandatory.** Every `EvidenceItem` must include the verbatim text from the source. Fact-Checker re-fetches and verifies. If you can't get the quote, the item doesn't enter the ledger.

6. **Each agent's output is JSON.** Agents communicate via structured Pydantic models. Free-text intermediate outputs are forbidden — they break the verification chain.

7. **The Reviewer can demand revisions.** If the rendered PDF contains a claim that doesn't trace to evidence, Author re-drafts. Max 3 iterations, then escalate.

## Code style

- Python 3.11+, type hints throughout, Pydantic for all I/O models.
- Each agent in its own file under `agents/`. One class per file, inheriting from `BaseAgent`.
- Tests in `tests/`. Each agent gets a unit test + at least one integration test in the pipeline.
- No silent exceptions. Log + re-raise or return a structured error in the agent's output.
- LLM calls go through a single `LLMClient` wrapper so model choice and retries are centralized.

## Tech stack reminders

- Anthropic SDK: `claude-sonnet-4-6` for Disambiguator/Researcher/Estimator/Fact-Checker, `claude-opus-4-7` for Author/Reviewer.
- SQLite for dev, Postgres for prod. Use SQLAlchemy.
- ReportLab + matplotlib + pdf2image for PDF rendering — these are already proven in `templates/pdf_builder.py`. Don't rewrite, parameterize.

## Working with the visual template

`templates/pdf_builder.py` is the proven 4-page visual layout. It currently has hardcoded data; the task is to make it accept a `BriefData` Pydantic model (defined by Author's output schema). Don't redesign the visual — match v0 output 1:1, just driven by data.

When adapting, preserve:
- Hero badge in top-left (replaces decorative avatar)
- 4 stat blocks below quick-take
- Career/company trajectory chart (matplotlib timeline)
- Who-they-are + Personal/Company-DNA cards on page 1
- Industries chips + geographic footprint + strategic signals cards on page 2
- Engagement readiness scorecard + key signals + recommended approach + discovery questions on page 3
- Recap stats + next steps + sources grid + methodology on page 4

Add: graceful handling for `None`/empty fields. If chips list is empty, omit the section. If badge value is null, render "INSUFFICIENT DATA" in gray.

## When testing

- Use the same 5 entities for regression: pick a public person + private person + public company + private company + LATAM/non-English entity.
- Compare every brief to its ledger. Diff any claim not in ledger.
- Spot-check 3 random `raw_quote` fields by manually opening the source.

## What success looks like

A run for "BW Project Management Dominican Republic" produces:
- A PDF where the industry chip grid has 4–6 chips (not 12), the geographic footprint shows the 1 country actually evidenced (not 5 fabricated), and the revenue badge has a visible caveat ("Estimated from...").
- A ledger with 15+ items, ≥90% verified by Fact-Checker.
- A reviewer report with `status: approved` on first or second iteration.

## Anti-patterns to refuse

If a user asks Claude Code to:
- "Just fill in the missing industries with reasonable guesses" → refuse, point to rule 2.
- "Drop the citation tags for cleaner code" → refuse, point to rule 1.
- "Use a point estimate instead of a range" → refuse, point to rule 3.
- "Add a quick LLM call in Author to fill in missing data" → refuse, that's the failure mode of v0.

## File map

See `SPEC.md` section 12.
