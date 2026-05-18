"""Tests for the web-search contact_finder fallback."""
from __future__ import annotations

import logging
from uuid import uuid4

from account_research.schemas.brief import ContactItem
from account_research.schemas.entity import Entity, EntityType
from account_research.tools.contact_finder import (
    _WebContact,
    _WebContactList,
    find_contacts_via_web,
    merge_contacts,
)


class _FakeLLM:
    """Minimal stand-in for LLMClient that returns a canned schema instance."""

    def __init__(self, canned=None, raise_exc: Exception | None = None):
        self.canned = canned
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def complete_with_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.canned


# ---------------------------------------------------------------------------
# find_contacts_via_web
# ---------------------------------------------------------------------------


def test_returns_empty_when_llm_returns_empty_list() -> None:
    fake = _FakeLLM(canned=_WebContactList(contacts=[]))
    entity = Entity(id=uuid4(), name="Ghost Co", type=EntityType.COMPANY)
    out = find_contacts_via_web(entity, fake)
    assert out == []
    # We DO call the LLM — empty result is information.
    assert len(fake.calls) == 1


def test_returns_empty_when_llm_raises() -> None:
    fake = _FakeLLM(raise_exc=RuntimeError("Sonnet refused"))
    entity = Entity(id=uuid4(), name="Acme", type=EntityType.COMPANY)
    out = find_contacts_via_web(entity, fake)
    assert out == []


def test_happy_path_returns_contactitem_with_web_source() -> None:
    canned = _WebContactList(contacts=[
        _WebContact(
            name="Jane Doe", title="CEO & Co-Founder",
            linkedin_url="https://www.linkedin.com/in/jane-doe",
            source_url="https://acme.com/team",
        ),
        _WebContact(
            name="John Roe", title="CTO",
            source_url="https://acme.com/team",
        ),
    ])
    fake = _FakeLLM(canned=canned)
    entity = Entity(id=uuid4(), name="Acme", type=EntityType.COMPANY)
    out = find_contacts_via_web(entity, fake)
    assert len(out) == 2
    assert all(isinstance(c, ContactItem) for c in out)
    assert all(c.source == "web_research" for c in out)
    # Web search never produces email / phone.
    assert all(c.email is None and c.phone is None for c in out)
    assert out[0].linkedin_url is not None
    assert out[1].linkedin_url is None


def test_caps_at_max_contacts() -> None:
    canned = _WebContactList(contacts=[
        _WebContact(name=f"Person {i}", source_url="https://example.com/team")
        for i in range(5)
    ])
    fake = _FakeLLM(canned=canned)
    entity = Entity(id=uuid4(), name="Acme", type=EntityType.COMPANY)
    out = find_contacts_via_web(entity, fake, max_contacts=3)
    assert len(out) == 3
    assert [c.name for c in out] == ["Person 0", "Person 1", "Person 2"]


def test_passes_correct_tools_and_agent_name() -> None:
    fake = _FakeLLM(canned=_WebContactList(contacts=[]))
    entity = Entity(id=uuid4(), name="X", type=EntityType.COMPANY)
    find_contacts_via_web(entity, fake)
    call = fake.calls[0]
    assert call["agent"] == "contact_finder"
    tool_types = [t.get("type") for t in call["extra_tools"]]
    # Server-side tool type identifiers — must include both.
    assert any("web_search" in t for t in tool_types)
    assert any("web_fetch" in t for t in tool_types)


# ---------------------------------------------------------------------------
# merge_contacts
# ---------------------------------------------------------------------------


def _c(name: str, source: str = "apollo", email: str | None = None) -> ContactItem:
    return ContactItem(name=name, source=source, email=email)


def test_merge_concats_when_no_overlap() -> None:
    apollo = [_c("Jane Doe", "apollo", "jane@x.com")]
    web = [_c("John Roe", "web_research")]
    out = merge_contacts(apollo, web)
    names = [c.name for c in out]
    assert names == ["Jane Doe", "John Roe"]


def test_merge_dedupes_case_insensitive_keeps_primary() -> None:
    apollo = [_c("Jane Doe", "apollo", "jane@x.com")]
    web = [_c("jane doe", "web_research")]  # dup, different case
    out = merge_contacts(apollo, web)
    assert len(out) == 1
    # Primary (Apollo) entry wins — keeps the email field.
    assert out[0].source == "apollo"
    assert out[0].email == "jane@x.com"


def test_merge_respects_max_contacts_cap() -> None:
    apollo = [_c("A"), _c("B"), _c("C")]
    web = [_c("D"), _c("E")]
    out = merge_contacts(apollo, web, max_contacts=3)
    assert [c.name for c in out] == ["A", "B", "C"]


def test_merge_preserves_primary_order_first() -> None:
    apollo = [_c("CEO"), _c("CTO")]
    web = [_c("VP Sales")]
    out = merge_contacts(apollo, web)
    assert [c.name for c in out] == ["CEO", "CTO", "VP Sales"]


def test_merge_empty_primary_just_returns_secondary() -> None:
    web = [_c("Jane Doe", "web_research"), _c("John Roe", "web_research")]
    out = merge_contacts([], web)
    assert [c.name for c in out] == ["Jane Doe", "John Roe"]
