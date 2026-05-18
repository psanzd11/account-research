"""Bright Data connector — scaffold.

Bright Data provides residential-proxy scraping with stealth headers and
CAPTCHA solving. When wired in, this connector would:

  - Route fetches of JS-rendered or anti-bot-protected pages
    (e.g. bwpm.pro under Cloudflare) through their proxy network
  - Return EvidenceItems with raw_quote extracted from the rendered DOM
  - source_type = "official_site" (the real source, not the proxy)
  - confidence  = same as direct fetch would have given (high for official)

This is the recommended path for sites that block both httpx and Anthropic's
server-side web_fetch (item 1 of this sprint hit that ceiling on bwpm.pro).

To enable: export BRIGHT_DATA_TOKEN=brd_xxxxx (and BRIGHT_DATA_ZONE if using
a specific zone). SDK: raw httpx against Bright Data's scraping browser
endpoint, or their `brightdata` Python package.
"""
from __future__ import annotations

from account_research.schemas.entity import Entity
from account_research.schemas.evidence import EvidenceItem
from account_research.tools.connectors.base import BaseConnector


class BrightDataConnector(BaseConnector):
    name = "bright_data"
    env_var = "BRIGHT_DATA_TOKEN"

    def _search_configured(
        self, query: str, entity: Entity | None
    ) -> list[EvidenceItem]:
        raise NotImplementedError(
            "Bright Data connector is scaffolded but not yet wired. "
            "Implement _search_configured against Bright Data's scraping "
            "browser endpoint — see module docstring."
        )
