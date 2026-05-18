"""Tests for the page-1 contacts panel:

- `ContactItem` schema (validation, optional fields).
- `ApolloConnector` contact path: seniority ranking + person→contact mapping.
- PDF renderer: `_section_contacts` produces flowables when populated,
  empty list when not.
"""
from __future__ import annotations

import logging
from unittest.mock import patch
from uuid import uuid4

import pytest

from account_research.schemas.brief import (
    BriefData,
    ContactItem,
    HeroSection,
    QuickTake,
)
from account_research.schemas.entity import Entity, EntityType
from account_research.tools.connectors.apollo import ApolloConnector


# ---------------------------------------------------------------------------
# ContactItem schema
# ---------------------------------------------------------------------------


def test_contact_minimal_only_name() -> None:
    c = ContactItem(name="Jane Doe")
    assert c.name == "Jane Doe"
    assert c.title is None and c.email is None and c.phone is None
    assert c.source == "apollo"


def test_contact_full() -> None:
    c = ContactItem(
        name="John Roe", title="CEO",
        email="john@example.com", phone="+1-555-0100",
        linkedin_url="https://www.linkedin.com/in/john-roe",
        source="apollo",
        source_url="https://www.linkedin.com/in/john-roe",
    )
    assert c.email == "john@example.com"
    assert str(c.linkedin_url).startswith("https://www.linkedin.com/")


def test_briefdata_contacts_capped_at_three() -> None:
    from pydantic import ValidationError
    too_many = [{"name": f"P{i}"} for i in range(4)]
    with pytest.raises(ValidationError):
        BriefData(
            entity_id=uuid4(),
            hero=HeroSection(name="Acme", entity_type=EntityType.COMPANY),
            quick_take=QuickTake(body="x"),
            contacts=too_many,
        )


def test_briefdata_contacts_default_empty() -> None:
    b = BriefData(
        entity_id=uuid4(),
        hero=HeroSection(name="Acme", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body="x"),
    )
    assert b.contacts == []


# ---------------------------------------------------------------------------
# Apollo connector — seniority ranking & mapping
# ---------------------------------------------------------------------------


def test_search_contacts_skipped_for_person_entity() -> None:
    e = Entity(id=uuid4(), name="Jane Doe", type=EntityType.PERSON)
    out = ApolloConnector().search_contacts(e)
    assert out == []


def test_search_contacts_skipped_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    e = Entity(id=uuid4(), name="Acme", type=EntityType.COMPANY)
    out = ApolloConnector().search_contacts(e)
    assert out == []


def test_rank_seniority_orders_correctly() -> None:
    conn = ApolloConnector()
    people = [
        {"title": "Senior Software Engineer"},
        {"title": "CEO"},
        {"title": "Vice President of Sales"},
        {"title": "Co-Founder & CTO"},
        {"title": "Marketing Manager"},
    ]
    ranked = sorted(people, key=conn._rank_seniority)
    titles = [p["title"] for p in ranked]
    # Co-Founder beats CEO ("co-founder" is index 1, "ceo" is index 2).
    # Then CEO, then VP, then the non-matching ones at the end.
    assert titles[0] == "Co-Founder & CTO"
    assert titles[1] == "CEO"
    assert titles[2] == "Vice President of Sales"
    assert titles[-1] in ("Senior Software Engineer", "Marketing Manager")


def test_person_to_contact_full_payload() -> None:
    conn = ApolloConnector()
    person = {
        "name": "Jane Roe", "title": "Chief Executive Officer",
        "email": "jane@acme.com",
        "sanitized_phone": "+1-415-555-0100",
        "linkedin_url": "https://www.linkedin.com/in/jane-roe",
        "id": "p_123",
    }
    c = conn._person_to_contact(person)
    assert c is not None
    assert c.name == "Jane Roe"
    assert c.title == "Chief Executive Officer"
    assert c.email == "jane@acme.com"
    assert c.phone == "+1-415-555-0100"
    assert str(c.linkedin_url).startswith("https://www.linkedin.com/")


def test_person_to_contact_filters_unlock_placeholder() -> None:
    """Apollo free-tier returns placeholders like `email_not_unlocked@domain.com`."""
    conn = ApolloConnector()
    person = {
        "name": "Mystery Person",
        "title": "CEO",
        "email": "email_not_unlocked@apollo.io",
    }
    c = conn._person_to_contact(person)
    assert c is not None
    assert c.email is None  # placeholder filtered


def test_person_to_contact_first_last_name_fallback() -> None:
    conn = ApolloConnector()
    person = {
        "first_name": "Alex", "last_name": "Plasencia",
        "title": "General Counsel",
    }
    c = conn._person_to_contact(person)
    assert c is not None
    assert c.name == "Alex Plasencia"


def test_person_to_contact_returns_none_without_name() -> None:
    conn = ApolloConnector()
    assert conn._person_to_contact({"title": "Ghost"}) is None


def test_search_contacts_ranks_and_caps(monkeypatch) -> None:
    """End-to-end: monkeypatch _post, verify ranking + top_n cap."""
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    conn = ApolloConnector(logger=logging.getLogger("test"))
    fake_response = {"people": [
        {"name": "Bob Engineer", "title": "Senior Engineer",
         "email": "bob@acme.com"},
        {"name": "Alice CEO", "title": "CEO",
         "email": "alice@acme.com"},
        {"name": "Carl Co-Founder", "title": "Co-Founder",
         "email": "carl@acme.com"},
        {"name": "Dora VP", "title": "Vice President of Engineering",
         "email": "dora@acme.com"},
    ]}
    e = Entity(id=uuid4(), name="Acme", type=EntityType.COMPANY)
    with patch.object(ApolloConnector, "_post", return_value=fake_response):
        out = conn.search_contacts(e, top_n=3)
    assert len(out) == 3
    names = [c.name for c in out]
    assert "Carl Co-Founder" == names[0]
    assert "Alice CEO" == names[1]
    assert "Dora VP" == names[2]
    assert "Bob Engineer" not in names  # ranked out


# ---------------------------------------------------------------------------
# PDF renderer — section omission + rendering
# ---------------------------------------------------------------------------


def test_section_contacts_empty_returns_empty() -> None:
    from account_research.designer.pdf_builder import _section_contacts
    assert _section_contacts([]) == []


def test_section_contacts_renders_flowables() -> None:
    from account_research.designer.pdf_builder import _section_contacts
    cs = [
        ContactItem(name="Jane Doe", title="CEO", email="j@x.com",
                    phone="+1-555-0100"),
        ContactItem(name="John Roe", title="COO"),
    ]
    flowables = _section_contacts(cs)
    assert len(flowables) > 0
    # Header label is added via _section_header (returns 2 flowables).
    # The body grid + caveat + spacer should follow.
    assert any(hasattr(f, "build") or hasattr(f, "drawOn") or hasattr(f, "wrap")
               for f in flowables)
