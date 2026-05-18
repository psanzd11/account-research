# Account Research Brief — Multi-Agent System

**Version:** 0.1 (initial spec)
**Status:** Design, ready for implementation
**Owner:** [your name]

---

## 1. What This Is

A pipeline of specialized AI agents that generates a 4-page visual PDF research brief on a person or company. The brief includes financial badge (net worth for persons, est. revenue for companies), career/company trajectory, deal-readiness scorecard, recommended approach, and discovery questions.

The visual template already exists and is proven (see `templates/pdf_builder.py`). The problem this system solves is **precision**, not formatting.

## 2. Why This Exists

A single LLM doing retrieve + estimate + write + layout in one pass hallucinates. Concrete failures observed in v0 (manual generation):

- **Fabricated financial figures**: A net worth range of "$15–30M USD" given without methodology, simply chosen by the model to fit the badge slot.
- **Padded visualizations**: A "12-industry" chip grid filled with extrapolated industries because the layout had 12 slots, when only 6 industries were verifiable.
- **Fabricated geographic counts**: A footprint map with "Panama: 5 projects, Jamaica: 3, LATAM: 8" — none of those counts came from any source.
- **Conflated time-period facts**: Employee/revenue figures from different years presented as current.
- **Inferences stated as facts**: Family relationships, organizational roles, and quotes presented at higher confidence than the underlying evidence supported.

This system makes those failures **structurally impossible** by separating roles, requiring citations for every claim, and refusing to render content without evidence backing.

## 3. Architecture

See `templates/pipeline_diagram.png` for the visual.

```
[1 Disambiguator] → [2 Researcher] → [3 Estimator] → [4 Fact-Checker]
                                                            ↓
                                                     evidence_ledger
                                                            ↓
[5 Author] → [6 Designer] → [7 Reviewer] ─── revision loop ──┘
                                                  │
                                            (max 3 iterations)
```

**Shared infrastructure**: Evidence Ledger (DB), Methodology Library (YAML), Confidence Taxonomy (enum).

---

## 4. Agent Specifications

Every agent is a Python class implementing `BaseAgent`:

```python
class BaseAgent(ABC):
    @abstractmethod
    def run(self, input_payload: dict, context: PipelineContext) -> dict: ...
```

Communication is via JSON. Each agent has a strict input/output schema (JSON Schema or Pydantic). Schemas live in `schemas/`.

### 4.1 Disambiguator

**Purpose**: Resolve "BW Management Group of DR" → exact entity (URL, name, type) before any research happens.

**Input**:
```json
{ "query": "string", "entity_type_hint": "person | company | unknown" }
```

**Output**:
```json
{
  "entity_id": "uuid",
  "name": "BW Project Management",
  "type": "company",
  "primary_url": "https://bwpm.pro",
  "aliases": ["BW Project Management", "bwpm.rd"],
  "confidence": "verified | high | medium | low",
  "candidates_rejected": [
    { "name": "BW Management LLC", "url": "...", "reason": "Not Dominican" }
  ]
}
```

**Behavior**:
- Run 2–4 web searches to surface candidates.
- If exactly one strong candidate matches the query AND geography hint: return it with `confidence: high`.
- If multiple candidates score similarly: return `status: "ambiguous"` with the top 3 in `candidates`. Pipeline halts; user picks.
- Never proceed on assumption.

**Tools**: `web_search`.

**Failure modes**: ambiguous → halt. No matches → return `status: "not_found"`, no fallback to a "best guess."

---

### 4.2 Researcher

**Purpose**: Build the `evidence_ledger` — a structured list of every fact relevant to the brief, each with a source.

**Input**: Disambiguator output.

**Output**: Array of `EvidenceItem`:
```json
{
  "id": "uuid",
  "entity_id": "uuid",
  "claim": "Founded in 2020 in the Dominican Republic",
  "category": "company_facts | financial | leadership | products | clients | geography | recognition | other",
  "source_url": "https://bwpm.pro/about/",
  "source_type": "official_site | sec_filing | press | linkedin | news | social | aggregator | other",
  "raw_quote": "Desde nuestros inicios en 2020 en la República Dominicana...",
  "fetched_at": "2026-05-08T17:00:00Z",
  "confidence": "verified | high | medium | low",
  "notes": "Company self-description; may include marketing inflation"
}
```

**Behavior**:
- Tier 1 sources (official site, SEC, gov registries) first.
- Tier 2 (LinkedIn, Crunchbase, news within last 24mo).
- Tier 3 (industry directories, blog mentions) only if tiers 1–2 are sparse.
- **Every item must include a `raw_quote` (verbatim from source) and a `source_url`**. No quote → not in ledger.
- Marketing claims from the company's own site get `confidence: medium` (likely true, may be inflated). Independent reporting gets `high`. Multi-source confirmation gets `verified`.
- Returns 15–40 items typically. Below 8 = entity has insufficient public footprint; surface this in the reviewer report.

**Tools**: `web_search`, `web_fetch`, optionally Apollo / Bright Data / LinkedIn Sales Nav if configured.

**Failure modes**: insufficient evidence (< 8 items) → flag in output but don't halt; downstream agents will leave sections empty.

---

### 4.3 Estimator

**Purpose**: Compute financial and team-size estimates from indirect signals in the ledger, using **named, versioned recipes** from the Methodology Library.

**Input**: ledger + entity metadata.

**Output**:
```json
{
  "estimates": [
    {
      "metric": "estimated_net_worth",
      "applies_to": "person",
      "value_range": "$15-30M USD",
      "confidence": "low",
      "method_id": "net_worth_individual_v1",
      "signals_used": [
        { "evidence_id": "uuid-1", "signal": "YPO_CDMX_membership", "weight": 0.4 },
        { "evidence_id": "uuid-2", "signal": "multi_venture_founder", "weight": 0.3 }
      ],
      "assumptions": [
        "YPO CDMX requires controlling a company with >$10M revenue",
        "Founder equity in 4 ventures averages ~15% per venture",
        "No public disclosure available"
      ],
      "caveat_text": "Privately held; not publicly disclosed. Estimate based on indirect signals."
    }
  ]
}
```

**Behavior**:
- Looks up `method_id` in `methodology/` directory. Loads the YAML recipe.
- Recipe specifies required input signals. If signals not present in ledger → returns `status: "insufficient_signals"`, no value.
- **Never generates a specific number without a recipe.** No improvisation.
- Output is always a **range**, never a point estimate.
- Always attaches `caveat_text` that will appear next to the figure in the final brief.

**Tools**: none (pure computation over ledger).

**Failure modes**: missing required signals → return null with reason. Author downstream renders "Insufficient public data" in the badge slot rather than a fake number.

---

### 4.4 Fact-Checker

**Purpose**: Verify each `EvidenceItem` is genuinely in its source. Catches: (a) Researcher hallucinations (quote was made up), (b) stale/dead links, (c) quotes that don't actually exist on the page.

**Input**: ledger.

**Output**: ledger with each item annotated:
```json
{
  ...EvidenceItem,
  "verification": {
    "status": "verified | unverifiable | contradicted | source_dead",
    "method": "exact_match | semantic_match | url_404 | content_changed",
    "checked_at": "2026-05-08T17:30:00Z"
  }
}
```

**Behavior**:
- Re-fetch each `source_url`.
- For each item: search the fetched page for `raw_quote` (exact substring match, then fuzzy ≥0.85).
- If quote found → `verified`.
- If quote not found → `unverifiable`. Item is *kept in ledger* but marked. Author will skip it.
- If source is 404/timeout → `source_dead`. Same treatment.
- Output a report: `{total: N, verified: V, unverifiable: U, dead: D}`. If V/N < 0.7, flag pipeline for review.

**Tools**: `web_fetch`.

**Failure modes**: rate-limited → wait + retry up to 3x. Persistent failures → log and mark items as `unverifiable`, don't fail pipeline.

---

### 4.5 Author

**Purpose**: Draft the brief content section-by-section, with every claim tagged to its `evidence_id`.

**Input**: verified ledger + estimates.

**Output**:
```json
{
  "hero": {
    "name": "BW Project Management",
    "tagline": "Project Management Consultancy · Santo Domingo, DR · bwpm.pro",
    "badge": {
      "label": "EST. REVENUE",
      "value": "$1-3M",
      "unit": "USD/yr",
      "caveat": "Revenue estimated from...",
      "method_id": "consulting_firm_revenue_v1"
    }
  },
  "quick_take": {
    "body": "Dominican PM consultancy founded 2020...",
    "best_angle": "Lead with ClickUp/MS Dynamics 365...",
    "evidence_ids": ["uuid-1", "uuid-2", "uuid-7"]
  },
  "stats": [
    { "value": "2020", "label": "FOUNDED IN RD", "evidence_id": "uuid-1" },
    { "value": "50+", "label": "PROJECTS DELIVERED", "evidence_id": "uuid-3" },
    ...
  ],
  "timeline": [...],
  "who_they_are_cards": [...],
  "industries": ["Technology", "Retail", "Finance", "Airlines"],
  "geographic_footprint": [
    { "location": "Dominican Republic (HQ)", "evidence_id": "uuid-1" }
  ],
  "scorecard": [
    { "metric": "Reachability", "score": 5, "rationale": "...", "evidence_ids": [...] }
  ],
  "discovery_questions": [...],
  "sources": [...]
}
```

**Behavior rules** (these are the anti-hallucination guarantees):
1. **No claim without `evidence_id`.** Every sentence/figure in the output traces to ≥1 ledger item.
2. **No padding.** If `industries` has 6 verified entries, the list is 6 long. If it has 0, the field is `[]` and the section will be omitted by Designer.
3. **No invented numbers.** Counts in `geographic_footprint` are derived from explicit mentions in the ledger or omitted. Never generated to "fit the visual."
4. **Estimates always carry caveats.** Badge values always include `caveat` text.
5. **Empty sections are honest.** If `who_they_are_cards` has < 2 entries, the section renders as a single card or is omitted — not padded with extrapolations.

**Tools**: none (LLM reasoning over structured input).

**Failure modes**: insufficient ledger for a section → return empty/partial output for that section. Don't fail the pipeline.

---

### 4.6 Designer

**Purpose**: Render the PDF from the Author's structured output.

**Input**: Author output.

**Output**: PDF file path + render report.

**Behavior**:
- Loads the visual template (`templates/pdf_builder.py`).
- Maps Author output → template parameters.
- Handles "Unknown" / empty sections gracefully:
  - Badge with `value: null` → renders "INSUFFICIENT DATA" instead of "$X-Y"
  - Empty chip grid → omits the section entirely
  - < 4 cards → renders 2 cards or omits the section
- After rendering, calls `pdf2image` to rasterize each page and runs visual heuristics:
  - No page should be < 50% full (orphan content)
  - No section header without content beneath it
  - Footer alignment check
- If checks fail, returns `status: "layout_issue"` with details.

**Tools**: ReportLab (already in template), matplotlib, pdf2image.

**Failure modes**: layout overflow → tighten spacers and re-render up to 2x, then return whatever fits with a warning.

---

### 4.7 Reviewer

**Purpose**: Final QA. Read the rendered PDF the way a human would and verify nothing on it lacks ledger backing.

**Input**: Final PDF + ledger + Author output.

**Output**:
```json
{
  "status": "approved | revision_required",
  "issues": [
    {
      "severity": "critical | warning | info",
      "location": "page 1, hero badge",
      "claim": "$1-3M USD/yr",
      "issue": "Method 'consulting_firm_revenue_v1' marked confidence 'low' but no caveat visible in rendered PDF",
      "suggested_fix": "Add caveat text below badge"
    }
  ],
  "iteration": 1
}
```

**Behavior**:
- Reads PDF (text + image via Claude vision).
- For each claim visible on the rendered page, checks: does it correspond to an `evidence_id` or `estimate.method_id`?
- Catches things the earlier agents might have missed (e.g., Author output had caveats but Designer dropped them).
- If `status: revision_required`, returns to Author with the issues. Max 3 iterations, then escalate to human.

**Tools**: PDF reading (text + vision), ledger query.

**Failure modes**: after 3 revisions still failing → output PDF + full issue list, mark `status: "human_review_needed"`.

---

## 5. Shared Infrastructure

### 5.1 Evidence Ledger (database)

**Storage**: SQLite for dev, Postgres for production.

**Schema**:
```sql
CREATE TABLE entities (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT CHECK (type IN ('person', 'company')),
  primary_url TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE evidence_items (
  id UUID PRIMARY KEY,
  entity_id UUID REFERENCES entities(id),
  claim TEXT NOT NULL,
  category TEXT,
  source_url TEXT NOT NULL,
  source_type TEXT,
  raw_quote TEXT NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL,
  confidence TEXT CHECK (confidence IN ('verified', 'high', 'medium', 'low')),
  verification_status TEXT,
  verification_checked_at TIMESTAMPTZ,
  notes TEXT
);

CREATE TABLE estimates (
  id UUID PRIMARY KEY,
  entity_id UUID REFERENCES entities(id),
  metric TEXT NOT NULL,
  value_range TEXT,
  confidence TEXT,
  method_id TEXT NOT NULL,
  assumptions JSONB,
  signals_used JSONB,
  caveat_text TEXT,
  computed_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE pipeline_runs (
  id UUID PRIMARY KEY,
  entity_id UUID REFERENCES entities(id),
  status TEXT,
  started_at TIMESTAMPTZ DEFAULT now(),
  completed_at TIMESTAMPTZ,
  iterations INT DEFAULT 0,
  final_pdf_path TEXT,
  issues JSONB
);
```

**API**: simple Python module `ledger.py` with functions `insert_evidence`, `get_evidence_for_entity`, `mark_verified`, etc.

### 5.2 Methodology Library

**Location**: `methodology/*.yaml`. One file per recipe.

**Format**: see `methodology/net_worth_individual_v1.yaml` (included in this repo).

**Versioning**: append `_v1`, `_v2` etc. Never modify a version in place; create a new version.

### 5.3 Confidence Taxonomy

| Level | Definition | Required for |
|---|---|---|
| `verified` | ≥2 independent sources concur | Promoting facts to "headline" status |
| `high` | 1 official source (company SEC filing, gov registry, person's own bio on official site) | Most facts in the brief |
| `medium` | 1 secondary source (news article, LinkedIn) | OK for non-headline facts |
| `low` | Indirect signal, marketing claim, undated source | Only if no better available; estimates default here |
| `estimated` | Computed by Estimator from signals via a recipe | Financial figures, team size |
| `unknown` | Couldn't find | Section omitted in brief |

**Rule**: any element rendered in the PDF must be `medium` or above, OR be an `estimated` with visible caveat. `low` items get included only with disclaimer text. `unknown` items are *never* rendered (no padding).

---

## 6. PDF Template

**File**: `templates/pdf_builder.py`. Already implemented (see code in repo).

**Adaptation needed** for v1:
1. Parameterize all currently-hardcoded data via a single `BriefData` Pydantic model that mirrors Author's output schema.
2. Add graceful handling for `None` / empty values:
   - Badge with `value=None` → render gray "INSUFFICIENT DATA" text instead of figure
   - Empty `chips` → omit section entirely
   - `cards < 4` → render whatever count there is, possibly in a 2- or 1-col layout
3. Render confidence indicators next to estimated values (small icon or color).
4. Add a final "Confidence Report" footer line on page 4 with counts: "47 facts, 41 verified, 6 estimated, 0 unverifiable."

---

## 7. Implementation Plan

| Sprint | Scope | Deliverables | Acceptance |
|---|---|---|---|
| **1** | Disambiguator + Researcher | `agents/disambiguator.py`, `agents/researcher.py`, `ledger.py`, SQLite schema | Run on 5 known entities (3 persons, 2 companies). Each yields 15+ ledger items, every item has source_url + raw_quote. Manual spot-check: 0 fabricated quotes. |
| **2** | Estimator + Fact-Checker + Methodology Library | 3 recipes (net_worth_individual_v1, saas_revenue_v1, consulting_firm_revenue_v1), `agents/estimator.py`, `agents/fact_checker.py` | For 5 entities where ground truth is known (publicly listed): estimator range contains the real value 4/5 times. Fact-checker catches injected fake quotes 5/5. |
| **3** | Author + Designer | `agents/author.py`, refactored `pdf_builder.py` accepting `BriefData`, render pipeline | End-to-end PDF generated for 1 entity. Every visible claim has corresponding ledger ID in the Author output. |
| **4** | Reviewer + Orchestrator + revision loop | `agents/reviewer.py`, `orchestrator.py`, retry/escalation logic | Generate brief for the 5 entities from sprint 1. Reviewer catches and corrects ≥80% of injected errors. |
| **5** | Optional: enrichment connectors | Apollo / Bright Data / LinkedIn Sales Nav integrations in Researcher | Average confidence per ledger rises from `medium` to `high`. |

---

## 8. Stack

```
Python 3.11+
anthropic SDK (Claude Sonnet for most agents, Opus for Author + Reviewer)
pydantic (schemas)
sqlalchemy + sqlite/postgres (ledger)
reportlab + matplotlib + pdf2image (PDF)
pyyaml (methodology library)
httpx (web fetching with caching)
pytest (tests)
```

**Setup**: `pip install -r requirements.txt`, set `ANTHROPIC_API_KEY`, run `python -m account_research.cli "BW Project Management"`.

---

## 9. Anti-Patterns (do NOT do)

These are real failures from v0 (manual generation). Each must be structurally impossible in v1:

1. **Filling layout slots with extrapolated data.** If the template shows 12 industry chips but the ledger only supports 6, render 6.
2. **Presenting estimates as facts.** Every financial figure must be paired with `caveat_text` from the recipe.
3. **Conflating time periods.** Each evidence item carries `fetched_at`; if an item is older than 12 months, Author should de-emphasize it.
4. **Inferring relationships not stated.** "Cousin of [CEO]" requires explicit evidence — implicit sharing of a surname is not enough.
5. **Generating quotes.** Researcher fabricating `raw_quote` is the failure mode Fact-Checker exists to catch. If verification rate drops below 70%, the run is rejected.
6. **Silent failure.** Empty sections must be rendered visibly ("Insufficient public data") so the reader knows what's missing, not papered over.

---

## 10. Example Workflow (BWPM, end-to-end)

```
User: "BW Project Management of Dominican Republic"

[1] Disambiguator
  → searches "BW Management Dominicana" + "BW Project Management Dominican"
  → finds 3 candidates: BW Project Management (bwpm.pro), B&W Group, BW Management LLC
  → BW Project Management matches geo + name strongest
  → confidence: high; return entity_id

[2] Researcher
  → fetches bwpm.pro/, bwpm.pro/about/, bwpm.pro/servicios/*, IG @bwpm.rd
  → also searches PMI ATP directory, ClickUp partner page
  → returns 22 evidence items:
    - "Founded 2020 in DR" (confidence: high, source: bwpm.pro/about)
    - "PMI ATP" (confidence: high, source: PMI directory)
    - "ClickUp partner exclusive RD" (confidence: medium, source: bwpm.pro homepage; not yet confirmed on ClickUp side)
    - "50+ projects delivered" (confidence: medium, marketing claim)
    - "Industries: tech, retail, finance, airlines" (confidence: medium, source: about page)
    - ... 17 more

[3] Estimator
  → recipe: consulting_firm_revenue_v1
  → required signals: years_active, project_count, geography, partnership_count
  → all present
  → output: "$0.5–2M USD/yr", confidence: low
  → caveat: "Estimated from 4 yrs operation, ~50 projects, LATAM PM consultancy benchmarks ($30–60K avg project value). Privately held; not disclosed."

[4] Fact-Checker
  → re-fetches all 22 source URLs
  → 19 verified, 2 source_changed (bwpm subpages restructured), 1 unverifiable
  → V/N = 0.86 — pipeline continues

[5] Author
  → drafts brief sections
  → industries chip grid: 4 chips (the 4 verified industries), NOT padded to 12
  → geographic_footprint: only "Dominican Republic (HQ)" with evidence_id, all other countries omitted
  → badge uses Estimator output verbatim with caveat
  → 47 sentences total, each with evidence_id

[6] Designer
  → renders PDF
  → industries section collapses to single row of 4 chips
  → footprint map renders single dot for DR; other slots omitted
  → confidence report footer: "22 facts, 19 verified, 2 source-changed, 1 unverifiable, 1 estimated"

[7] Reviewer
  → reads PDF
  → checks: badge "$0.5–2M USD/yr" → traces to estimate.method_id 'consulting_firm_revenue_v1' ✓
  → checks: "Founded 2020" → traces to evidence_id uuid-... ✓
  → checks: footprint map shows only DR → no claim about other countries ✓
  → no issues → status: approved
```

Compare this to v0 output: 4 industries vs 12, 1 country vs 5, $0.5–2M vs $1–3M (with methodology). Each one no longer fabricated.

---

## 11. Out of Scope (for v1)

- Persons-vs-companies templating divergence beyond what's needed (the template handles both with field swaps).
- Multi-language. English output only. Spanish entities are researched in Spanish but the brief is in English.
- Real-time updates. Each brief is a point-in-time snapshot.
- User-defined methodology recipes. Recipes are added by maintainers, not end users.
- Custom branding per user. Single visual template.

---

## 12. Files in This Repo

```
account-research-agents/
├── SPEC.md                              ← this file
├── CLAUDE.md                            ← conventions for Claude Code
├── README.md                            ← quick start
├── templates/
│   ├── pdf_builder.py                   ← proven visual template (from v0)
│   └── pipeline_diagram.png             ← architecture diagram
├── methodology/
│   ├── net_worth_individual_v1.yaml
│   ├── consulting_firm_revenue_v1.yaml
│   └── saas_revenue_v1.yaml
└── examples/
    └── sample_ledger.json               ← example evidence ledger
```
