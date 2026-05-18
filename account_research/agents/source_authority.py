"""Source authority classifier — gates the Researcher away from aggregator-
heavy ledgers and lets the Reviewer enforce a Tier-1 minimum per category.

Why this exists: the v1 Researcher prompt treated Tier 2 and Tier 3 sources
as roughly equivalent. In practice the model often filled batches with
aggregator data (theorg.com, rocketreach, contactout) that just rehashes
LinkedIn. Verification rate looked high but real source diversity was low.

Tiers (1 = highest authority, 3 = lowest):
  Tier 1 — primary sources:
    - SEC filings, government registries
    - The entity's own official site (for self-facts only)
    - Mainstream business press (NYT, WSJ, Bloomberg, Reuters, FT, Forbes,
      Axios, The Information, regional equivalents)
    - Conference proceedings, podcast transcripts with named hosts

  Tier 2 — secondary primary:
    - Trade press, industry publications
    - Wikipedia / Wikidata (for biographical anchor)
    - Press releases from the entity
    - LinkedIn (when reachable, not aggregator-scraped)

  Tier 3 — aggregators / scraped data:
    - theorg.com, rocketreach.co, contactout.com, zoominfo.com,
      apollo.io scraping, signalhire, lusha
    - Crunchbase aggregator pages (their own analysis is Tier 2)
    - PitchBook public summary pages
    - Generic professional-network scrapers
"""
from __future__ import annotations

import os
from typing import Iterable
from urllib.parse import urlparse

from account_research.schemas.evidence import SourceType, VerifiedEvidenceItem

# Feature flag (Round 2 / G1). Default ON.
_AUTHORITY_GATING_ENABLED = (
    os.environ.get("SOURCE_AUTHORITY_GATING", "1").lower()
    not in ("0", "false", "no", "off")
)

# Maximum share of a batch that can come from aggregator sources before the
# post-validator caps them. Overridable for tuning.
MAX_AGGREGATOR_SHARE = float(
    os.environ.get("MAX_AGGREGATOR_SHARE", "0.25")
)

# Hosts known to publish aggregator/scraped data.
KNOWN_AGGREGATORS: set[str] = {
    "theorg.com",
    "rocketreach.co",
    "contactout.com",
    "zoominfo.com",
    "signalhire.com",
    "lusha.com",
    "apollo.io",            # the public scraping pages, not the API
    "rocketreach.com",
    "leadiq.com",
    "kendo.com",
    "snov.io",
    "hunter.io",
    "swordfish.ai",
}

# Mainstream business press / wires. Hosts only — used by `tier_of`.
KNOWN_TIER1_PRESS: set[str] = {
    # US / global business press
    "nytimes.com", "wsj.com", "bloomberg.com", "reuters.com",
    "ft.com", "forbes.com", "businessweek.com",
    "axios.com", "theinformation.com", "fortune.com",
    "economist.com", "barrons.com", "marketwatch.com", "cnbc.com",
    "bbc.com", "bbc.co.uk", "guardian.co.uk", "theguardian.com",
    # Spanish-speaking LATAM business press
    "expansion.mx", "americaeconomia.com", "eluniversal.com.mx",
    "lanacion.com.ar", "clarin.com", "infobae.com",
    "elfinanciero.com.mx", "diariolibre.com",
    "elcaribe.com.do", "diariohispaniola.com",
    "estrategiaynegocios.net", "elobservador.com.uy",
    "valor.com.br", "exame.com",
    # Tier-1 industry / trade press
    "techcrunch.com", "wired.com", "stratechery.com",
}

# Government / regulatory registries (always Tier 1).
KNOWN_GOV_HOSTS: set[str] = {
    "sec.gov",
    "edgar.sec.gov",
    "companieshouse.gov.uk",
    "uspto.gov",
    "registro-empresas.gov.do",  # DR registry
    "datos.gob.mx",
    "registroscivilcr.go.cr",
    "ifc.org",  # IFC disclosures
    "worldbank.org",
    "imf.org",
}

# Wikipedia / Wikidata — Tier 2 biographical anchor.
KNOWN_WIKI_HOSTS: set[str] = {
    "en.wikipedia.org", "es.wikipedia.org", "pt.wikipedia.org",
    "fr.wikipedia.org", "wikidata.org",
}


def _host(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return h.split(":", 1)[0]


def _match_any(host: str, hosts: Iterable[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def is_aggregator(url: str, source_type: SourceType | None = None) -> bool:
    """True if the URL host is on the known-aggregator list OR source_type
    is `AGGREGATOR`."""
    if source_type == SourceType.AGGREGATOR:
        return True
    return _match_any(_host(url), KNOWN_AGGREGATORS)


def tier_of(url: str, source_type: SourceType | None = None) -> int:
    """Return 1 (highest authority), 2, or 3 for a (url, source_type) pair.

    - SEC filings, gov registries, mainstream press, the entity's own
      official_site → 1
    - Wikipedia, trade press, LinkedIn (non-aggregator), press releases,
      podcasts/conferences → 2
    - Known aggregators, source_type=AGGREGATOR or SOCIAL → 3
    """
    host = _host(url)

    if source_type == SourceType.SEC_FILING:
        return 1
    if source_type == SourceType.GOV_REGISTRY:
        return 1
    if source_type == SourceType.OFFICIAL_SITE:
        return 1

    if _match_any(host, KNOWN_GOV_HOSTS):
        return 1
    if _match_any(host, KNOWN_TIER1_PRESS):
        return 1

    if _match_any(host, KNOWN_AGGREGATORS):
        return 3
    if source_type == SourceType.AGGREGATOR:
        return 3
    if source_type == SourceType.SOCIAL:
        return 3

    if _match_any(host, KNOWN_WIKI_HOSTS):
        return 2
    if source_type == SourceType.LINKEDIN:
        return 2
    if source_type == SourceType.PRESS:
        return 2  # not a Tier-1 press host but still press
    if source_type == SourceType.NEWS:
        return 2

    return 2  # default — unknown but not an aggregator


def aggregator_share(items: list[VerifiedEvidenceItem]) -> float:
    """Fraction of verified items that came from an aggregator host."""
    verified = [i for i in items if i.verification.status == "verified"]
    if not verified:
        return 0.0
    n = sum(1 for ev in verified if is_aggregator(str(ev.source_url), ev.source_type))
    return n / len(verified)


def tier_breakdown(
    items: list[VerifiedEvidenceItem],
) -> dict[int, int]:
    """Return {tier: count} for verified items."""
    out: dict[int, int] = {1: 0, 2: 0, 3: 0}
    for ev in items:
        if ev.verification.status != "verified":
            continue
        t = tier_of(str(ev.source_url), ev.source_type)
        out[t] = out.get(t, 0) + 1
    return out


def tier1_count_per_category(
    items: list[VerifiedEvidenceItem],
) -> dict[str, int]:
    """Return {category: tier1_count} across verified items."""
    out: dict[str, int] = {}
    for ev in items:
        if ev.verification.status != "verified":
            continue
        if tier_of(str(ev.source_url), ev.source_type) != 1:
            continue
        c = ev.category.value
        out[c] = out.get(c, 0) + 1
    return out


def cap_aggregators_if_exceeded(
    items: list[VerifiedEvidenceItem],
    *,
    max_share: float = MAX_AGGREGATOR_SHARE,
) -> tuple[list[VerifiedEvidenceItem], int]:
    """If aggregator items exceed `max_share` of verified items, drop the
    lowest-confidence aggregator items until under the cap.

    Returns (kept_items, dropped_count).
    """
    if not _AUTHORITY_GATING_ENABLED:
        return items, 0
    verified = [i for i in items if i.verification.status == "verified"]
    if not verified:
        return items, 0

    aggregators = [
        ev for ev in verified
        if is_aggregator(str(ev.source_url), ev.source_type)
    ]
    target_max = int(max_share * len(verified))
    if len(aggregators) <= target_max:
        return items, 0

    # Sort aggregators worst-first: low > medium > high > verified
    conf_order = {"low": 0, "medium": 1, "high": 2, "verified": 3,
                  "estimated": 4, "unknown": 5}
    aggregators.sort(key=lambda e: conf_order.get(e.confidence.value, 0))

    to_drop = aggregators[: len(aggregators) - target_max]
    drop_ids = {ev.id for ev in to_drop}
    return [ev for ev in items if ev.id not in drop_ids], len(drop_ids)
