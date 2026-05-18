"""Methodology library: YAML parses; unregistered recipes raise."""
from __future__ import annotations

import pytest

from account_research.methodology.loader import (
    dispatch,
    is_registered,
    load_recipe,
)


def test_consulting_firm_yaml_parses():
    r = load_recipe("consulting_firm_revenue_v1")
    assert r.id == "consulting_firm_revenue_v1"
    assert r.applies_to == "company"
    assert r.minimum_required_signals >= 1
    assert any(s.signal == "years_in_operation" for s in r.required_signals)


def test_net_worth_yaml_parses():
    r = load_recipe("net_worth_individual_v1")
    assert r.id == "net_worth_individual_v1"
    assert r.applies_to == "person"
    assert "{signals_summary}" in r.caveat_template or r.caveat_template


def test_unknown_yaml_raises():
    with pytest.raises(FileNotFoundError):
        load_recipe("does_not_exist_v9")


def test_dispatch_to_unregistered_recipe_raises():
    """Dispatching to a method_id that no Python module registered must error,
    not silently fall back to inline computation (CLAUDE.md rule 4)."""
    from uuid import uuid4

    assert not is_registered("phantom_recipe_v9")
    with pytest.raises(KeyError):
        dispatch("phantom_recipe_v9", entity_id=uuid4(), ledger=[])


def test_sprint2_recipes_are_registered():
    """After Sprint 2 all 3 recipes have Python implementations registered."""
    from account_research.methodology import recipes  # noqa: F401 — triggers registration

    for mid in ("consulting_firm_revenue_v1", "net_worth_individual_v1", "saas_revenue_v1"):
        assert is_registered(mid), f"{mid} should be registered"
