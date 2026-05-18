"""Researcher — build the evidence ledger for one resolved entity.

SPEC §4.2. Every item carries a verbatim raw_quote + source_url. Below 8 items
the batch is flagged low_evidence; downstream agents will leave sections empty
rather than pad (CLAUDE.md rule 2).

Feature flag RESEARCHER_COVERAGE_LOOP (default ON): if any required category
is empty after the first batch, run one supplemental round focused on the
gaps.
"""
from __future__ import annotations

import json
import os
from uuid import uuid4

from account_research.agents.base import BaseAgent, PipelineContext
from account_research.llm_client import SONNET
from account_research.schemas.entity import Entity
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceBatch,
    EvidenceCategory,
    EvidenceItem,
)
from account_research.tools.connectors.apollo import ApolloConnector
from account_research.tools.web import web_fetch_tool_def, web_search_tool_def


LOW_EVIDENCE_THRESHOLD = 8


# Ordering used by dedup_evidence to pick the "best" duplicate. Lower index
# wins: a verified item beats a high-confidence item, etc.
_CONFIDENCE_RANK = {
    ConfidenceLevel.VERIFIED: 0,
    ConfidenceLevel.HIGH: 1,
    ConfidenceLevel.MEDIUM: 2,
    ConfidenceLevel.LOW: 3,
    ConfidenceLevel.ESTIMATED: 4,
    ConfidenceLevel.UNKNOWN: 5,
}


def _normalize_claim(claim: str) -> str:
    """Collapse whitespace, lower-case, strip surrounding punctuation.

    Used as the dedup hash component so trivial reformattings of the same
    claim don't survive as separate items.
    """
    return " ".join(claim.strip().lower().split())


def dedup_evidence(items: list[EvidenceItem]) -> list[EvidenceItem]:
    """Collapse items sharing `(normalize(claim), str(source_url))` into one.

    The kept variant is the highest-confidence one (VERIFIED > HIGH > MEDIUM > …);
    when confidence is tied, the first seen wins so order remains stable.

    Same claim from *different* URLs is preserved — that's cross-source
    corroboration, not duplication.
    """
    best: dict[tuple[str, str], EvidenceItem] = {}
    for item in items:
        key = (_normalize_claim(item.claim), str(item.source_url))
        prev = best.get(key)
        if prev is None:
            best[key] = item
            continue
        if _CONFIDENCE_RANK.get(item.confidence, 9) < _CONFIDENCE_RANK.get(
            prev.confidence, 9
        ):
            best[key] = item
    return list(best.values())

_COVERAGE_LOOP_ENABLED = (
    os.environ.get("RESEARCHER_COVERAGE_LOOP", "1").lower()
    not in ("0", "false", "no", "off")
)

# Categories the brief CANNOT render meaningfully without ≥1 item.
REQUIRED_CATEGORIES = [
    EvidenceCategory.COMPANY_FACTS,
    EvidenceCategory.FINANCIAL,
    EvidenceCategory.LEADERSHIP,
    EvidenceCategory.PRODUCTS,
    EvidenceCategory.CLIENTS,
    EvidenceCategory.GEOGRAPHY,
]

# Source-discovery hints per category — appended to the supplemental prompt
# so the model knows where to look when a category came back empty.
_CATEGORY_HINTS: dict[EvidenceCategory, str] = {
    EvidenceCategory.COMPANY_FACTS: (
        "founding year, legal entity name, headquarters, ownership "
        "(check official site About page, gov registry, Wikipedia, Crunchbase)"
    ),
    EvidenceCategory.FINANCIAL: (
        "revenue, funding rounds, valuation, profitability "
        "(check SEC filings, Crunchbase, PitchBook, press releases, 10-K)"
    ),
    EvidenceCategory.LEADERSHIP: (
        "CEO, founders, key executives, board members "
        "(check LinkedIn, About page, press releases, board listings)"
    ),
    EvidenceCategory.PRODUCTS: (
        "core product lines, service offerings, platform features "
        "(check product pages, Solutions page, case studies, press releases)"
    ),
    EvidenceCategory.CLIENTS: (
        "named customers, case studies, partner directories "
        "(check Clients/Customers page, Case Studies page, partner pages)"
    ),
    EvidenceCategory.GEOGRAPHY: (
        "offices, countries served, jurisdictions "
        "(check Locations page, footer of official site, Wikipedia)"
    ),
}


SYSTEM_PROMPT = """\
You are the **Researcher** agent in a citation-grounded research pipeline.

Your job is to build an evidence ledger for ONE entity: a structured list of
every public fact relevant to a research brief, each item carrying a
verbatim quote from its source. Downstream agents are forbidden from writing
any sentence that doesn't cite one of your items.

SOURCE TIERS — STRICT (the Reviewer rejects briefs that lean on aggregators)
- Tier 1 (target ≥1 per required category): the entity's own official site,
  SEC filings, gov registries (IFC, EDGAR, Companies House, gov.do, etc.),
  mainstream business press (Bloomberg, WSJ, NYT, Reuters, FT, Forbes,
  Expansión MX, América Economía, Diario Libre, Valor Brasil), conference
  proceedings, podcast transcripts with named hosts.
- Tier 2 (acceptable, mix freely): Wikipedia / Wikidata, LinkedIn (when
  reachable as the entity's own profile, not via aggregator), trade press,
  press releases from the entity, industry analyst pieces.
- Tier 3 (aggregator scraping — MAX 25% OF BATCH): theorg.com,
  rocketreach.co, contactout.com, zoominfo.com, signalhire, lusha,
  hunter.io, swordfish.ai. These rehash LinkedIn data. ALLOWED only to
  corroborate facts you already have from Tier 1 / 2 sources. If your
  batch is dominated by Tier 3 sources, the Reviewer will demand revision.

WHEN STARTING A RESEARCH ROUND:
- First pass: hit Tier 1 sources for each required category. Try the
  entity's own site, search for "[entity name] Bloomberg|Reuters|press",
  and check Wikipedia / Wikidata.
- Second pass: fill remaining gaps with Tier 2. Be precise about queries.
- Only fall to Tier 3 (aggregators) if Tier 1 and Tier 2 returned nothing
  for a fact. Treat aggregator items as suspect — they often hallucinate
  job titles and dates.

For each candidate fact you find:
1. fetch the source page if you haven't already (use web_fetch),
2. locate the exact passage that supports the claim,
3. copy that passage VERBATIM into raw_quote — every character must match
   what the page contains. The downstream Fact-Checker re-fetches and does
   a substring search; if your quote isn't on the page, the item is
   discarded and your verification rate drops.

EVIDENCE ITEM FIELDS
- claim: a short factual sentence in English, even if the source is Spanish
- category: one of company_facts | financial | leadership | products |
  clients | geography | recognition | team | other
- source_url: the exact URL the quote was fetched from
- source_type: official_site | sec_filing | gov_registry | press | linkedin |
  news | social | aggregator | other
- raw_quote: verbatim text from the source. Quotes in the original language
  are fine. If you can't get a verbatim quote (e.g. the source is a PDF you
  can't extract from), DO NOT include the item.
- confidence:
    - "verified": confirmed across ≥2 independent sources
    - "high": one official source (the entity's own site for self-facts,
      SEC for filings, gov registry, etc.)
    - "medium": one secondary source (news, LinkedIn) OR a marketing claim
      from the entity's own site (likely true, may be inflated)
    - "low": indirect signal, undated source, social-media inference
- notes: optional. Useful for flagging "marketing claim, treat as approximate"
  or "explicitly enumerated — do NOT extrapolate beyond this list".

CRITICAL ANTI-PATTERNS (the rules that this whole system exists to enforce)
- NEVER fabricate a quote. If you didn't see it on the page, it doesn't exist.
- NEVER invent a number to fit a story. "50+ projects" is fine if the page
  literally says "more than 50 projects"; otherwise it isn't.
- NEVER list more industries / countries / clients than the source explicitly
  enumerates. If a page says "tech, retail, finance, airlines", include only
  those four — not "tech, retail, finance, airlines, banking, energy".
- If you see "10+ industries" but only 6 are named, include the 6 with a
  note that says only 6 are explicitly named.

CATEGORY COVERAGE — aim for ≥1 item per required category
The downstream brief renders sections per category. If a category is empty
its section is omitted, which can leave the brief looking thin. Required
categories with hints on where to find them:
  - company_facts: founding year, HQ, ownership (About page, gov registry)
  - financial: revenue, funding, valuation (SEC filings, Crunchbase, press)
  - leadership: CEO, founders, board (LinkedIn, About page, board listings)
  - products: product lines, features (Products page, case studies)
  - clients: named customers (Clients page, case studies, partner pages)
  - geography: offices, countries served (Locations page, footer, Wikipedia)
Allocate at least one web_search query specifically targeting each required
category before exhausting your budget on a single dimension.

TARGET VOLUME
Aim for 20 to 40 items. Below 8 indicates the entity has insufficient public
footprint — set low_evidence_flag=true and proceed with what you have.

WHEN THE PRIMARY URL IS BLOCKED OR LOGIN-WALLED
LinkedIn profiles, some corporate intranets, and login-gated pages return
empty/redirect content via web_fetch. Don't give up. Pivot to:
  - the person's company website(s) or about pages
  - press coverage, conference talks, podcast appearances
  - YPO/EO/Vistage directories, alumni lists, university bios
  - Wikipedia / Wikidata in the entity's locale language
Run more web_search calls with new angles (company name, "interview",
"founder of", university name) before falling back to an empty batch.

OUTPUT
End your turn by calling the emit_evidencebatch tool exactly once with the
complete batch. The entity_id field must match the entity_id you were given.
"""


def _coverage_gaps(batch: EvidenceBatch) -> list[EvidenceCategory]:
    """Return the required categories that have zero items in `batch`."""
    present = {item.category for item in batch.items}
    return [c for c in REQUIRED_CATEGORIES if c not in present]


def _supplemental_prompt(entity: Entity, gaps: list[EvidenceCategory]) -> str:
    """Build the user message for a focused supplemental research round."""
    bullet_hints = "\n".join(
        f"  - {c.value}: {_CATEGORY_HINTS.get(c, '(no hints)')}" for c in gaps
    )
    primary = str(entity.primary_url) if entity.primary_url else "(no primary URL)"
    return (
        f"Entity: {entity.name} ({entity.type.value})\n"
        f"Primary URL: {primary}\n\n"
        f"The first research batch returned ZERO items for the following "
        f"required categories. Run additional web_search + web_fetch calls "
        f"focused on these gaps, then emit a SUPPLEMENTAL EvidenceBatch with "
        f"only the new items (do NOT repeat items from the first batch).\n\n"
        f"Categories needing coverage:\n{bullet_hints}\n\n"
        f"Same rules as before: verbatim raw_quote, no fabrication, no padding."
    )


class ResearcherAgent(BaseAgent[Entity, EvidenceBatch]):
    name = "researcher"
    model = SONNET
    input_schema = Entity
    output_schema = EvidenceBatch

    def run(self, payload: Entity, ctx: PipelineContext) -> EvidenceBatch:
        llm = ctx.require_llm()

        # G7: pre-seed Apollo evidence (silently skipped if APOLLO_API_KEY unset).
        apollo_items = []
        try:
            apollo_items = ApolloConnector(logger=ctx.logger).search(
                query=payload.name, entity=payload,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("Apollo connector raised %s — continuing", exc)

        primary = str(payload.primary_url) if payload.primary_url else "(no primary URL known)"
        aliases = ", ".join(payload.aliases) if payload.aliases else "(none)"

        apollo_block = ""
        if apollo_items:
            preview = [
                {"category": ev.category.value, "claim": ev.claim,
                 "raw_quote": ev.raw_quote[:120]}
                for ev in apollo_items
            ]
            apollo_block = (
                f"\nPRE-SEEDED EVIDENCE (Apollo CRM, {len(apollo_items)} items, "
                f"already saved to the ledger — DO NOT re-emit these):\n"
                f"{json.dumps(preview, ensure_ascii=False, indent=2)}\n\n"
                "Treat the items above as known anchors. Use your web_search "
                "budget to corroborate them against primary sources (press, "
                "SEC, official site) and to find facts they do not cover.\n"
            )

        user_msg = (
            f"Entity to research:\n"
            f"  entity_id: {payload.id}\n"
            f"  name: {payload.name}\n"
            f"  type: {payload.type.value}\n"
            f"  primary_url: {primary}\n"
            f"  aliases: {aliases}\n"
            f"{apollo_block}\n"
            "Build the evidence ledger. Use web_search to discover sources, "
            "web_fetch to retrieve them, then call emit_evidencebatch with the "
            "complete list. Every item MUST include a verbatim raw_quote."
        )

        batch = llm.complete_with_json(
            model=self.model,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            schema=EvidenceBatch,
            extra_tools=[
                web_search_tool_def(max_uses=8),
                web_fetch_tool_def(max_uses=15),
            ],
            max_tokens=16384,
            temperature=0.0,
            agent=self.name,
        )

        # Defensive: enforce entity_id consistency + low-evidence flag.
        if batch.entity_id != payload.id:
            ctx.logger.warning(
                "Researcher returned entity_id=%s but expected %s — overriding",
                batch.entity_id,
                payload.id,
            )
            batch = batch.model_copy(update={"entity_id": payload.id})

        # Models pick deterministic UUID-ish strings (e.g. a1b2c3d4-0001-...)
        # which collide across runs and break UNIQUE constraints. Regenerate
        # every id locally and force entity_id to match the resolved entity.
        merged_items = list(apollo_items) + [
            item.model_copy(update={"id": uuid4(), "entity_id": payload.id})
            for item in batch.items
        ]
        batch = batch.model_copy(update={"items": merged_items})
        if apollo_items:
            ctx.logger.info(
                "Researcher: prepended %d Apollo item(s) to LLM batch (total now %d)",
                len(apollo_items), len(merged_items),
            )

        # C1: supplemental round if any required category is empty
        if _COVERAGE_LOOP_ENABLED:
            gaps = _coverage_gaps(batch)
            if gaps:
                ctx.logger.info(
                    "Researcher: required-category gaps %s — running supplemental round",
                    [g.value for g in gaps],
                )
                extras = self.run_supplemental(
                    payload,
                    user_prompt=_supplemental_prompt(payload, gaps),
                    ctx=ctx,
                )
                if extras:
                    merged = list(batch.items) + extras
                    batch = batch.model_copy(update={"items": merged})
                    ctx.logger.info(
                        "Researcher: supplemental round added %d items "
                        "(total now %d)", len(extras), len(merged),
                    )
                    still_missing = _coverage_gaps(batch)
                    if still_missing:
                        ctx.logger.warning(
                            "Researcher: still no coverage for %s after supplemental",
                            [g.value for g in still_missing],
                        )

        # Sprint 3.3 — collapse items sharing (normalized claim, source_url).
        # Same fact at the same URL was emitted multiple times when the model
        # cited the same paragraph across categories. Keep highest-confidence
        # variant; different sources for the same claim are preserved as
        # cross-source corroboration.
        before = len(batch.items)
        deduped = dedup_evidence(list(batch.items))
        if len(deduped) < before:
            ctx.logger.info(
                "Researcher: dedup collapsed %d → %d items (%d duplicate(s) removed)",
                before, len(deduped), before - len(deduped),
            )
            batch = batch.model_copy(update={"items": deduped})

        if len(batch.items) < LOW_EVIDENCE_THRESHOLD and not batch.low_evidence_flag:
            batch = batch.model_copy(update={"low_evidence_flag": True})

        return batch

    def run_supplemental(
        self,
        entity: Entity,
        *,
        user_prompt: str,
        ctx: PipelineContext,
    ) -> list[EvidenceItem]:
        """Run ONE focused research round with a caller-supplied prompt.

        Used internally by `run()` for the coverage loop and externally by
        `refine.py` for the user-triggered refinement flow. Returns the new
        items (NOT merged with anything else). Returns ``[]`` on any error
        so callers can keep going.

        IDs are regenerated locally; the caller is responsible for merging
        and dedup'ing against any pre-existing ledger.
        """
        llm = ctx.require_llm()
        try:
            supplemental = llm.complete_with_json(
                model=self.model,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                schema=EvidenceBatch,
                extra_tools=[
                    web_search_tool_def(max_uses=12),
                    web_fetch_tool_def(max_uses=20),
                ],
                max_tokens=16384,
                temperature=0.0,
                agent=self.name + ":supplemental",
            )
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("Researcher: supplemental round failed: %s", exc)
            return []
        if not supplemental.items:
            return []
        return [
            item.model_copy(update={"id": uuid4(), "entity_id": entity.id})
            for item in supplemental.items
        ]
