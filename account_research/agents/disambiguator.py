"""Disambiguator — resolve an ambiguous query to one concrete entity.

SPEC §4.1. Halts on ambiguity (no fallback guess). Never proceeds on assumption.
"""
from __future__ import annotations

from uuid import uuid4

from account_research.agents.base import BaseAgent, PipelineContext
from account_research.llm_client import SONNET
from account_research.schemas.entity import DisambigInput, DisambigResult
from account_research.tools.web import web_search_tool_def


SYSTEM_PROMPT = """\
You are the **Disambiguator** agent in a citation-grounded research pipeline.

Your only job is to resolve a user-supplied query (a person or company name,
often partial or ambiguous) to ONE concrete real-world entity, with a primary
URL and confidence rating. You MUST NOT proceed on assumption.

PROTOCOL
1. Run 2 to 4 web searches to surface candidate entities. Vary your queries:
   include geography hints, industry hints, and known aliases.
2. Score each candidate on (a) name match, (b) geography/locale match,
   (c) alignment with the entity_type_hint, (d) recency and credibility of
   the surfacing source.
3. Decide:
   - If exactly one candidate is clearly the strongest match: return
     status="ok" with the resolved entity populated. Confidence "high" if the
     match is unambiguous; "verified" only if you confirmed across ≥2
     independent sources.
   - If two or more candidates are similarly plausible: return
     status="ambiguous" with up to 3 top candidates in candidates_top3.
     DO NOT pick a winner.
   - If no candidate plausibly matches: return status="not_found". DO NOT
     return a best guess.

OUTPUT
At the end of your turn you MUST call the emit_disambigresult tool exactly
once with the final DisambigResult. Always include candidates_rejected with
a one-line reason per rejected option — this is how a reviewer audits your
decision.

CITATION DISCIPLINE
You are part of a pipeline whose entire purpose is to make hallucination
structurally impossible. Never fabricate a URL. Never invent a company.
If unsure, return ambiguous or not_found.
"""


class DisambiguatorAgent(BaseAgent[DisambigInput, DisambigResult]):
    name = "disambiguator"
    model = SONNET
    input_schema = DisambigInput
    output_schema = DisambigResult

    def run(self, payload: DisambigInput, ctx: PipelineContext) -> DisambigResult:
        llm = ctx.require_llm()

        user_msg = (
            f"Query: {payload.query!r}\n"
            f"entity_type_hint: {payload.entity_type_hint}\n"
        )
        if payload.geography_hint:
            user_msg += f"geography_hint: {payload.geography_hint}\n"
        user_msg += (
            "\nFind the matching entity. Run searches as needed, then call "
            "emit_disambigresult with your verdict."
        )

        result = llm.complete_with_json(
            model=self.model,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            schema=DisambigResult,
            extra_tools=[web_search_tool_def(max_uses=4)],
            max_tokens=4096,
            temperature=0.0,
            agent=self.name,
        )

        # Models emit deterministic UUID-ish strings — regenerate locally so
        # they don't collide across runs (see [[recipes-as-python-modules]]).
        updates = {}
        if result.entity is not None:
            updates["entity"] = result.entity.model_copy(update={"id": uuid4()})
        if result.candidates_top3:
            updates["candidates_top3"] = [
                c.model_copy(update={"id": uuid4()}) for c in result.candidates_top3
            ]
        return result.model_copy(update=updates) if updates else result
