"""LinkedIn Sales Navigator connector — scaffold.

LinkedIn Sales Navigator's official API (Marketing Developer Platform or
Talent Solutions, depending on the org's contract) exposes:

  - Account profiles (companies)
  - Lead profiles (people, with employment history, education)
  - Activity signals (job changes, posts, mentions)

When wired in, this connector would map each profile into EvidenceItems:
  - source_url   = the linkedin.com/in/* or /company/* URL
  - source_type  = "linkedin"
  - raw_quote    = the verbatim field value (e.g. tenure dates, headline)
  - confidence   = "high" (LinkedIn-verified self-report)

LinkedIn API access requires partnership approval — not a self-serve key.
For lighter alternatives consider Bright Data's LinkedIn dataset connector
(routed via BrightDataConnector).

To enable: export LINKEDIN_SALES_NAV_TOKEN=ln_xxxxx
"""
from __future__ import annotations

from account_research.schemas.entity import Entity
from account_research.schemas.evidence import EvidenceItem
from account_research.tools.connectors.base import BaseConnector


class LinkedInSalesNavConnector(BaseConnector):
    name = "linkedin_sales_nav"
    env_var = "LINKEDIN_SALES_NAV_TOKEN"

    def _search_configured(
        self, query: str, entity: Entity | None
    ) -> list[EvidenceItem]:
        raise NotImplementedError(
            "LinkedIn Sales Nav connector is scaffolded but not yet wired. "
            "Implement _search_configured against the LinkedIn Marketing "
            "Developer Platform — see module docstring."
        )
