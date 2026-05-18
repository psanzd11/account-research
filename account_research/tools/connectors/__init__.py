from account_research.tools.connectors.apollo import ApolloConnector
from account_research.tools.connectors.base import BaseConnector
from account_research.tools.connectors.bright_data import BrightDataConnector
from account_research.tools.connectors.linkedin_sales_nav import LinkedInSalesNavConnector

# Order matters: the Researcher should try cheaper / faster connectors first.
ALL_CONNECTORS: list[type[BaseConnector]] = [
    ApolloConnector,
    LinkedInSalesNavConnector,
    BrightDataConnector,
]


def configured_connectors() -> list[BaseConnector]:
    """Return instances of every connector whose env var is set.

    Returns [] when nothing is configured — Researcher then behaves exactly
    as it does today (web tools only).
    """
    out: list[BaseConnector] = []
    for cls in ALL_CONNECTORS:
        c = cls()
        if c.is_configured():
            out.append(c)
    return out


__all__ = [
    "ALL_CONNECTORS",
    "ApolloConnector",
    "BaseConnector",
    "BrightDataConnector",
    "LinkedInSalesNavConnector",
    "configured_connectors",
]
