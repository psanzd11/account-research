"""Web-search fallback for finding outreach contacts at a company.

Used when the Apollo connector returns few/no contacts (typical on the free
Apollo tier or for entities outside Apollo's coverage — small LATAM firms,
non-English companies, etc.).

Cost: 1 Sonnet call with web_search + web_fetch tools, ~$0.05-0.20 per run.

Returns `ContactItem` objects with name + title + linkedin_url, NEVER with
email or phone — those don't appear verbatim on fetchable public pages.
The Designer's caveat ("verify before outreach") still applies.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from account_research.llm_client import LLMClient, SONNET
from account_research.schemas.brief import ContactItem
from account_research.schemas.entity import Entity
from account_research.tools.web import web_fetch_tool_def, web_search_tool_def


SYSTEM_PROMPT = """\
You research senior decision-makers at a given company using web_search and
web_fetch. Return 2-3 current top leaders with their NAME and TITLE.

WORKFLOW
1. Run web_search to locate the company's leadership/team/about page. Try:
   - "<company name>" leadership team
   - "<company name>" executive team OR founders
   - site:<company-domain> team OR leadership OR about
   - "<company name>" CEO OR CTO OR founder
2. Use web_fetch on the most promising URLs to extract names + titles
   VERBATIM from the page text.
3. If team pages are blocked or empty, pivot to press releases, news,
   LinkedIn search results, Crunchbase, or interviews.

RULES
- Prefer current leaders. Do NOT include former employees.
- Do NOT invent contacts. If you cannot find any, return an empty list.
- Names must come verbatim from a fetched page (no paraphrasing of titles).
- Limit to the top 3 most senior people you can find. Senior =
  founder/co-founder, CEO/President/COO/CFO/CTO/CMO/CRO, VP-level, or
  Head of <major function>.
- Each contact MUST include `name` and `source_url` (the page where you
  found it). `title` and `linkedin_url` are optional but include when known.
- Do NOT include emails or phones — they don't appear on public pages and
  guessing them is out of scope here.

OUTPUT
End your turn by calling emit_webcontactlist exactly once.
"""


class _WebContact(BaseModel):
    """Internal contact schema produced by the LLM tool call."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=80)
    title: str | None = Field(default=None, max_length=120)
    linkedin_url: HttpUrl | None = None
    source_url: HttpUrl


class _WebContactList(BaseModel):
    """Tool-call payload — list of contacts the LLM identified."""

    model_config = ConfigDict(extra="forbid")

    contacts: list[_WebContact] = Field(default_factory=list, max_length=5)


def find_contacts_via_web(
    entity: Entity,
    llm: LLMClient,
    *,
    max_contacts: int = 3,
    logger=None,
) -> list[ContactItem]:
    """Find up to `max_contacts` senior contacts for a company via web search.

    Safe to call on any entity / configuration — returns `[]` on:
      - non-company entities (web search for "team page" makes no sense)
      - LLM errors / refusals
      - empty result from the model

    Does NOT touch the evidence ledger or Fact-Checker. Output flows directly
    into `BriefData.contacts` as `source="web_research"`.
    """
    if logger is None:
        import logging
        logger = logging.getLogger("contact_finder")

    user_msg = (
        f"Company: {entity.name}\n"
        f"Primary URL: {entity.primary_url or '(unknown)'}\n\n"
        f"Find up to {max_contacts} senior decision-makers. Use web_search "
        f"to find the team/leadership page, then web_fetch to extract names "
        f"and titles. End with emit_webcontactlist."
    )

    try:
        result = llm.complete_with_json(
            model=SONNET,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            schema=_WebContactList,
            extra_tools=[
                web_search_tool_def(max_uses=4),
                web_fetch_tool_def(max_uses=5),
            ],
            max_tokens=4096,
            temperature=0.0,
            agent="contact_finder",
        )
    except Exception as exc:  # noqa: BLE001 — best-effort side-channel
        logger.warning("contact_finder: LLM call failed (%s) — returning []", exc)
        return []

    contacts: list[ContactItem] = []
    for wc in result.contacts[:max_contacts]:
        try:
            contacts.append(ContactItem(
                name=wc.name,
                title=wc.title,
                email=None,
                phone=None,
                linkedin_url=wc.linkedin_url,
                source="web_research",
                source_url=wc.source_url,
            ))
        except Exception as exc:  # noqa: BLE001 — drop malformed entries
            logger.warning(
                "contact_finder: dropped malformed contact %r (%s)",
                wc.name, exc,
            )
    logger.info(
        "contact_finder: returned %d contact(s) via web search", len(contacts)
    )
    return contacts


def merge_contacts(
    primary: list[ContactItem],
    secondary: list[ContactItem],
    *,
    max_contacts: int = 3,
) -> list[ContactItem]:
    """Concatenate primary + secondary, dedupe by lowercased name.

    Primary entries win on collision (they typically have richer fields —
    Apollo provides email/phone where the web fallback does not). Returns
    at most `max_contacts` items, preserving primary's ordering first.
    """
    seen: set[str] = set()
    out: list[ContactItem] = []
    for source in (primary, secondary):
        for c in source:
            key = c.name.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
            if len(out) >= max_contacts:
                return out
    return out
