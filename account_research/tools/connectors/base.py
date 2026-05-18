"""Connector base class.

Connectors are enterprise-data sources the Researcher can optionally consult:
Apollo (CRM), Bright Data (proxy-scraped pages), LinkedIn Sales Navigator,
etc. Each one reads its credentials from env vars; if unconfigured it
silently returns [] so the rest of the pipeline keeps working.

This is a scaffold — concrete API calls are deferred until a maintainer
wires in real credentials and the relevant SDK. The contract here is what
the Researcher would call against.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

from account_research.schemas.entity import Entity
from account_research.schemas.evidence import EvidenceItem


class BaseConnector(ABC):
    """Abstract connector. Subclasses set `name`, `env_var`, and implement
    `_search_configured`. The framework handles the configuration check and
    silent skip behavior."""

    name: str = "base"
    env_var: str = ""  # e.g. "APOLLO_API_KEY"

    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger(f"connector.{self.name}")

    def is_configured(self) -> bool:
        """True iff the env var holding our credential is set and non-empty."""
        return bool(os.environ.get(self.env_var, "").strip())

    def search(self, query: str, entity: Entity | None = None) -> list[EvidenceItem]:
        """Search this data source for facts about `query` / `entity`.

        Returns EvidenceItems with verbatim raw_quote. If not configured,
        returns [] and logs at INFO level so the caller knows it was skipped.
        """
        if not self.is_configured():
            self.logger.info(
                "Connector %s skipped: %s env var not set",
                self.name, self.env_var,
            )
            return []
        return self._search_configured(query, entity)

    @abstractmethod
    def _search_configured(
        self, query: str, entity: Entity | None
    ) -> list[EvidenceItem]:
        """Real implementation — only called when is_configured() is true."""
        ...
