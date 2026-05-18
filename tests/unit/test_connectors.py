"""Connector scaffold: configured-or-not gating, NotImplementedError for the
real call when configured, silent skip otherwise."""
from __future__ import annotations

import pytest

from account_research.tools.connectors import (
    ALL_CONNECTORS,
    ApolloConnector,
    BaseConnector,
    BrightDataConnector,
    LinkedInSalesNavConnector,
    configured_connectors,
)


def test_all_three_connectors_registered():
    names = {c.name for c in [ApolloConnector(), BrightDataConnector(), LinkedInSalesNavConnector()]}
    assert names == {"apollo", "bright_data", "linkedin_sales_nav"}


def test_all_connectors_inherit_base():
    for cls in ALL_CONNECTORS:
        assert issubclass(cls, BaseConnector)


def test_search_returns_empty_when_env_var_missing(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    monkeypatch.delenv("BRIGHT_DATA_TOKEN", raising=False)
    monkeypatch.delenv("LINKEDIN_SALES_NAV_TOKEN", raising=False)

    for cls in ALL_CONNECTORS:
        c = cls()
        assert c.is_configured() is False
        result = c.search("BWPM")
        assert result == []  # silent skip, no error


def test_search_raises_not_implemented_when_configured(monkeypatch):
    """When credentials are set, the scaffolded stubs raise NotImplementedError.
    Apollo is now WIRED (Round 2 / G7) so it doesn't raise — instead it
    attempts an HTTP call which returns [] when given an invalid entity."""
    from account_research.schemas.entity import Entity, EntityType
    for cls in ALL_CONNECTORS:
        c = cls()
        monkeypatch.setenv(c.env_var, "dummy_credential_for_test")
        assert c.is_configured() is True
        if cls is ApolloConnector:
            # Wired connector: will attempt HTTP, fail with auth error, return [].
            entity = Entity(name="ZZZ_nonexistent", type=EntityType.COMPANY)
            result = c.search("ZZZ_nonexistent", entity=entity)
            assert result == []  # auth fails → caught → [] returned
        else:
            with pytest.raises(NotImplementedError):
                c.search("anything")


def test_configured_connectors_filters_correctly(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    monkeypatch.delenv("BRIGHT_DATA_TOKEN", raising=False)
    monkeypatch.delenv("LINKEDIN_SALES_NAV_TOKEN", raising=False)
    assert configured_connectors() == []

    monkeypatch.setenv("APOLLO_API_KEY", "apk_test")
    enabled = configured_connectors()
    assert len(enabled) == 1
    assert enabled[0].name == "apollo"


def test_empty_env_var_is_treated_as_not_configured(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "")
    c = ApolloConnector()
    assert c.is_configured() is False

    monkeypatch.setenv("APOLLO_API_KEY", "   ")  # whitespace only
    assert c.is_configured() is False
