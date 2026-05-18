"""Apollo.io connector — wired (Round 2 / G7).

When `APOLLO_API_KEY` is set, this connector queries Apollo's REST API and
returns verified-shape EvidenceItem records that the Researcher merges into
its evidence ledger BEFORE running web_search. The intent is to pre-seed
HIGH-confidence anchors (employment, current title, education, headquarters)
so the Researcher can spend its web_search budget on cross-corroboration
and deeper press / SEC research instead of basic profile lookup.

Apollo organizes data into two relevant endpoints:
  POST /v1/mixed_people/search   — people lookup (q_organization_name,
                                    q_keywords, person_titles, etc.)
  POST /v1/mixed_companies/search — company lookup (q_keywords, organization
                                    names, domains, etc.)

We pull only the FIRST result (deduplicated to the entity at hand) and emit
EvidenceItems for the data points Apollo confirms. Apollo's data is itself
aggregated from public sources (LinkedIn, websites, news) so we tag it as
`source_type=aggregator` and `confidence=medium`. The Fact-Checker still
re-verifies whatever raw_quote we emit against the source_url Apollo cites.

Failures (HTTP errors, rate limits, malformed responses) log a warning and
return [] so the rest of the pipeline keeps working.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from account_research.schemas.brief import ContactItem
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceItem,
    SourceType,
)
from account_research.tools.connectors.base import BaseConnector

APOLLO_BASE = "https://api.apollo.io/v1"
APOLLO_TIMEOUT_SECONDS = 12.0


class ApolloConnector(BaseConnector):
    name = "apollo"
    env_var = "APOLLO_API_KEY"

    def _search_configured(
        self, query: str, entity: Entity | None
    ) -> list[EvidenceItem]:
        if entity is None:
            self.logger.info("Apollo: skipped (no entity context)")
            return []

        try:
            if entity.type == EntityType.COMPANY:
                return self._search_company(query, entity)
            if entity.type == EntityType.PERSON:
                return self._search_person(query, entity)
        except httpx.HTTPError as exc:
            self.logger.warning("Apollo HTTP error: %s", exc)
        except (KeyError, ValueError, TypeError) as exc:
            self.logger.warning("Apollo: malformed response (%s)", exc)
        return []

    # ------------------------------------------------------------------
    # Company path
    # ------------------------------------------------------------------

    def _search_company(self, query: str, entity: Entity) -> list[EvidenceItem]:
        payload = {
            "q_organization_name": entity.name or query,
            "page": 1,
            "per_page": 1,
        }
        org = self._post("/mixed_companies/search", payload)
        results = (org or {}).get("organizations") or []
        if not results:
            self.logger.info("Apollo: no company match for %r", entity.name)
            return []
        org_data = results[0]
        return self._company_to_items(org_data, entity)

    def _company_to_items(self, org: dict, entity: Entity) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []
        source_url = org.get("website_url") or f"{APOLLO_BASE}/organizations/{org.get('id', '')}"

        def add(claim: str, raw_quote: str, *,
                category: EvidenceCategory = EvidenceCategory.COMPANY_FACTS,
                source_type: SourceType = SourceType.AGGREGATOR,
                confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM,
                notes: str | None = "Pre-seeded from Apollo"):
            items.append(EvidenceItem(
                id=uuid4(),
                entity_id=entity.id,
                claim=claim,
                category=category,
                source_url=source_url,
                source_type=source_type,
                raw_quote=raw_quote,
                fetched_at=datetime.now(timezone.utc),
                confidence=confidence,
                notes=notes,
            ))

        if org.get("primary_domain"):
            add(
                claim=f"Primary domain is {org['primary_domain']}",
                raw_quote=f"primary_domain: {org['primary_domain']}",
            )
        if org.get("estimated_num_employees"):
            add(
                claim=f"Estimated {org['estimated_num_employees']} employees",
                raw_quote=f"estimated_num_employees: {org['estimated_num_employees']}",
                category=EvidenceCategory.TEAM,
            )
        if org.get("founded_year"):
            add(
                claim=f"Founded in {org['founded_year']}",
                raw_quote=f"founded_year: {org['founded_year']}",
            )
        if org.get("industry"):
            add(
                claim=f"Industry: {org['industry']}",
                raw_quote=f"industry: {org['industry']}",
            )
        if org.get("headquarters") or (org.get("city") and org.get("country")):
            hq = org.get("headquarters") or f"{org.get('city')}, {org.get('country')}"
            add(
                claim=f"Headquarters: {hq}",
                raw_quote=f"headquarters: {hq}",
                category=EvidenceCategory.GEOGRAPHY,
            )
        if org.get("annual_revenue"):
            # Apollo annual_revenue can be a string like "$1B-$10B" or a number
            add(
                claim=f"Annual revenue (Apollo estimate): {org['annual_revenue']}",
                raw_quote=f"annual_revenue: {org['annual_revenue']}",
                category=EvidenceCategory.FINANCIAL,
            )
        if org.get("total_funding"):
            add(
                claim=f"Total funding raised: ${org['total_funding']:,}",
                raw_quote=f"total_funding: {org['total_funding']}",
                category=EvidenceCategory.FINANCIAL,
            )

        self.logger.info(
            "Apollo: pre-seeded %d company evidence item(s) for %s",
            len(items), entity.name,
        )
        return items

    # ------------------------------------------------------------------
    # Person path
    # ------------------------------------------------------------------

    def _search_person(self, query: str, entity: Entity) -> list[EvidenceItem]:
        payload = {
            "q_keywords": entity.name or query,
            "page": 1,
            "per_page": 1,
        }
        result = self._post("/mixed_people/search", payload)
        people = (result or {}).get("people") or []
        if not people:
            self.logger.info("Apollo: no person match for %r", entity.name)
            return []
        return self._person_to_items(people[0], entity)

    def _person_to_items(self, person: dict, entity: Entity) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []
        source_url = person.get("linkedin_url") or (
            f"{APOLLO_BASE}/people/{person.get('id', '')}"
        )

        def add(claim: str, raw_quote: str, *,
                category: EvidenceCategory = EvidenceCategory.LEADERSHIP,
                source_type: SourceType = SourceType.AGGREGATOR,
                confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM,
                notes: str | None = "Pre-seeded from Apollo"):
            items.append(EvidenceItem(
                id=uuid4(),
                entity_id=entity.id,
                claim=claim,
                category=category,
                source_url=source_url,
                source_type=source_type,
                raw_quote=raw_quote,
                fetched_at=datetime.now(timezone.utc),
                confidence=confidence,
                notes=notes,
            ))

        title = person.get("title") or person.get("headline")
        if title:
            org = person.get("organization", {}) or {}
            org_name = org.get("name", "")
            add(
                claim=f"Current title: {title}" + (f" at {org_name}" if org_name else ""),
                raw_quote=f"title: {title}; organization: {org_name}",
            )
        if person.get("city") and person.get("country"):
            add(
                claim=f"Based in {person['city']}, {person['country']}",
                raw_quote=f"city: {person['city']}; country: {person['country']}",
                category=EvidenceCategory.GEOGRAPHY,
            )
        if person.get("employment_history"):
            top = person["employment_history"][:5]
            for emp in top:
                start = emp.get("start_date", "")[:4] or "?"
                end = emp.get("end_date", "")[:4] or "present"
                org = emp.get("organization_name") or ""
                role = emp.get("title") or ""
                if not org:
                    continue
                add(
                    claim=f"{role} at {org} ({start}–{end})" if role else f"{org} ({start}–{end})",
                    raw_quote=f"{role} | {org} | {start}–{end}",
                )

        self.logger.info(
            "Apollo: pre-seeded %d person evidence item(s) for %s",
            len(items), entity.name,
        )
        return items

    # ------------------------------------------------------------------
    # Contacts path — emits ContactItem directly (NOT via evidence ledger).
    # Contacts are PII records routed to BriefData.contacts; the Fact-Checker
    # is skipped because emails/phones don't appear verbatim on fetchable
    # public pages.
    # ------------------------------------------------------------------

    # Apollo titles that mark a high-seniority decision-maker. Order matters:
    # earlier roles outrank later ones in the tie-break inside _rank_seniority.
    _SENIORITY_KEYWORDS = (
        "founder", "co-founder", "ceo", "chief executive",
        "president",
        "coo", "cfo", "cto", "cmo", "cro", "cpo", "chief",
        "vp ", "vice president", "head of",
        "director",
    )

    def search_contacts(
        self,
        entity: Entity,
        *,
        top_n: int = 3,
    ) -> list[ContactItem]:
        """Return up to `top_n` outreach contacts for a company entity.

        Apollo's /mixed_people/search endpoint takes a list of titles; we
        request the union of seniority keywords and rank-sort what comes
        back. Returns ``[]`` for non-company entities, when the API key is
        not set, or on any HTTP error.
        """
        if entity.type != EntityType.COMPANY:
            return []
        if not self.is_configured():
            self.logger.info(
                "Apollo: search_contacts skipped (%s not set)", self.env_var
            )
            return []

        try:
            payload = {
                "q_organization_name": entity.name,
                "person_titles": list(self._SENIORITY_KEYWORDS),
                "page": 1,
                "per_page": max(top_n * 3, 10),  # over-fetch so we can rank
            }
            result = self._post("/mixed_people/search", payload)
        except httpx.HTTPError as exc:
            self.logger.warning("Apollo contacts: HTTP error %s", exc)
            return []
        except (KeyError, ValueError, TypeError) as exc:
            self.logger.warning("Apollo contacts: malformed response (%s)", exc)
            return []

        people = (result or {}).get("people") or []
        if not people:
            self.logger.info(
                "Apollo: no contacts matched for company %r", entity.name
            )
            return []

        ranked = sorted(people, key=self._rank_seniority)
        contacts: list[ContactItem] = []
        for person in ranked[:top_n]:
            item = self._person_to_contact(person)
            if item is not None:
                contacts.append(item)
        self.logger.info(
            "Apollo: returned %d contact(s) for %s", len(contacts), entity.name
        )
        return contacts

    def _rank_seniority(self, person: dict) -> int:
        title = (person.get("title") or person.get("headline") or "").lower()
        for i, kw in enumerate(self._SENIORITY_KEYWORDS):
            if kw in title:
                return i
        return len(self._SENIORITY_KEYWORDS)

    def _person_to_contact(self, person: dict) -> ContactItem | None:
        name = person.get("name") or " ".join(
            x for x in (person.get("first_name"), person.get("last_name")) if x
        )
        if not name:
            return None
        title = person.get("title") or person.get("headline")

        # Apollo returns email in `email` (verified plan) or `email_status`
        # placeholders (`email_not_unlocked@domain.com`) on free plans. We
        # only surface real-looking emails so the user isn't pinged with
        # un-actionable placeholders.
        raw_email = (person.get("email") or "").strip() or None
        email = raw_email if raw_email and "@" in raw_email and "_not_" not in raw_email else None

        phone = None
        for key in ("sanitized_phone", "mobile_phone", "phone"):
            v = person.get(key)
            if v:
                phone = str(v).strip()
                break
        # Also try the nested phone_numbers list (Apollo returns it on people
        # with verified contact info).
        if phone is None:
            pn = person.get("phone_numbers") or []
            if pn and isinstance(pn, list):
                first = pn[0] if isinstance(pn[0], dict) else None
                if first:
                    phone = (first.get("sanitized_number")
                             or first.get("raw_number"))

        linkedin = person.get("linkedin_url") or None
        source_url = linkedin or (
            f"{APOLLO_BASE}/people/{person.get('id', '')}" if person.get("id") else None
        )

        try:
            return ContactItem(
                name=name[:80],
                title=title[:120] if title else None,
                email=email,
                phone=phone[:40] if phone else None,
                linkedin_url=linkedin,
                source="apollo",
                source_url=source_url,
            )
        except Exception as exc:  # pydantic ValidationError, mostly bad URLs
            self.logger.warning(
                "Apollo: dropped malformed contact %r (%s)", name, exc
            )
            return None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _post(self, path: str, payload: dict) -> dict | None:
        headers = {
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "X-Api-Key": os.environ[self.env_var].strip(),
        }
        url = f"{APOLLO_BASE}{path}"
        with httpx.Client(timeout=APOLLO_TIMEOUT_SECONDS) as client:
            r = client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            return r.json()
