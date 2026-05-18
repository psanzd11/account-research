"""Prompt-caching wiring tests (A1).

Verifies that Author and Reviewer mark the right blocks with
``cache_control={"type": "ephemeral"}`` so Anthropic serves the static
system prompt + ledger from cache on revision iterations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from pathlib import Path
from uuid import uuid4

import pytest

from account_research.agents.author import AuthorAgent, AuthorInput
from account_research.agents.base import PipelineContext
from account_research.agents.reviewer import ReviewerAgent, ReviewInput
from account_research.schemas.brief import BriefData, HeroSection, QuickTake
from account_research.schemas.entity import Entity, EntityType
from account_research.schemas.evidence import (
    ConfidenceLevel,
    EvidenceCategory,
    SourceType,
    Verification,
    VerifiedEvidenceItem,
)
from account_research.schemas.review import ReviewerReport


@dataclass
class _FakeLLM:
    canned: Any
    captured: dict | None = None

    def complete_with_json(self, **kwargs):
        self.captured = kwargs
        return self.canned


def _minimal_brief(eid=None) -> BriefData:
    return BriefData(
        entity_id=eid or uuid4(),
        hero=HeroSection(name="TestCo", entity_type=EntityType.COMPANY),
        quick_take=QuickTake(body="A short take."),
    )


def _ev(entity_id) -> VerifiedEvidenceItem:
    return VerifiedEvidenceItem(
        entity_id=entity_id,
        claim="Some claim",
        category=EvidenceCategory.COMPANY_FACTS,
        source_url="https://example.com/p",
        source_type=SourceType.OFFICIAL_SITE,
        raw_quote="Some claim",
        fetched_at=datetime.now(timezone.utc),
        confidence=ConfidenceLevel.HIGH,
        verification=Verification(
            status="verified", method="exact_match",
            checked_at=datetime.now(timezone.utc), similarity=1.0,
        ),
    )


def _cache_control_blocks(blocks: list[dict]) -> list[dict]:
    return [b for b in blocks if isinstance(b, dict) and b.get("cache_control")]


class TestAuthorCacheControl:
    def test_system_prompt_is_cacheable(self):
        entity = Entity(name="X", type=EntityType.COMPANY)
        canned = _minimal_brief(entity.id)
        fake = _FakeLLM(canned=canned)
        AuthorAgent().run(
            AuthorInput(entity=entity, ledger=[_ev(entity.id)], estimates=[]),
            PipelineContext(llm_client=fake),
        )
        system = fake.captured["system"]
        assert isinstance(system, list), "system must be a block list for caching"
        marked = _cache_control_blocks(system)
        assert marked, "system prompt must carry cache_control={type:ephemeral}"
        assert marked[0]["cache_control"] == {"type": "ephemeral"}

    def test_user_content_splits_stable_and_dynamic_blocks(self):
        entity = Entity(name="X", type=EntityType.COMPANY)
        canned = _minimal_brief(entity.id)
        fake = _FakeLLM(canned=canned)
        AuthorAgent().run(
            AuthorInput(entity=entity, ledger=[_ev(entity.id)], estimates=[]),
            PipelineContext(llm_client=fake),
        )
        content = fake.captured["messages"][0]["content"]
        assert isinstance(content, list)
        assert len(content) >= 2
        # First block (entity + ledger + estimates) carries cache_control;
        # second block (revision feedback + final instruction) does NOT.
        assert content[0].get("cache_control") == {"type": "ephemeral"}
        assert "cache_control" not in content[1]
        assert "Verified ledger" in content[0]["text"]
        assert "Author the BriefData now" in content[1]["text"]

    def test_revision_feedback_lives_outside_cached_block(self):
        """Revision-mode issues must NOT be in the cached prefix, otherwise
        the cache key changes every iteration and cache hits stay at 0."""
        entity = Entity(name="X", type=EntityType.COMPANY)
        canned = _minimal_brief(entity.id)
        fake = _FakeLLM(canned=canned)
        from account_research.schemas.review import ReviewIssue
        AuthorAgent().run(
            AuthorInput(
                entity=entity, ledger=[_ev(entity.id)], estimates=[],
                previous_brief=_minimal_brief(entity.id),
                reviewer_issues=[ReviewIssue(
                    severity="critical", location="p1", claim="$X",
                    issue="Some issue text the cache must not see",
                )],
                iteration=2,
            ),
            PipelineContext(llm_client=fake),
        )
        content = fake.captured["messages"][0]["content"]
        cached_text = content[0]["text"]
        dynamic_text = content[1]["text"]
        assert "Some issue text the cache must not see" not in cached_text
        assert "Some issue text the cache must not see" in dynamic_text
        assert "REVISION MODE (iteration 2)" in dynamic_text


class TestReviewerCacheControl:
    def test_system_prompt_is_cacheable(self, monkeypatch, tmp_path):
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")
        monkeypatch.setattr(
            "account_research.agents.reviewer._extract_pdf_text_with_fallback",
            lambda _p, ctx: ("rendered text", []),
        )
        monkeypatch.setattr(
            "account_research.agents.reviewer._rasterize_pdf",
            lambda p: iter([]),
        )
        canned = ReviewerReport(status="approved", iteration=1, pdf_path=str(pdf))
        fake = _FakeLLM(canned=canned)
        ReviewerAgent().run(
            ReviewInput(brief=_minimal_brief(), ledger=[], estimates=[],
                        pdf_path=str(pdf), iteration=1),
            PipelineContext(llm_client=fake),
        )
        system = fake.captured["system"]
        assert isinstance(system, list)
        marked = _cache_control_blocks(system)
        assert marked, "Reviewer system prompt must be cacheable"

    def test_ledger_block_is_cached_brief_is_not(self, monkeypatch, tmp_path):
        """Ledger is stable across iterations → cached. Brief JSON + PDF text
        differ each iter (Author re-emits, Designer re-renders) → NOT cached.
        """
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")
        monkeypatch.setattr(
            "account_research.agents.reviewer._extract_pdf_text_with_fallback",
            lambda _p, ctx: ("rendered text body", []),
        )
        monkeypatch.setattr(
            "account_research.agents.reviewer._rasterize_pdf",
            lambda p: iter([]),
        )

        brief = _minimal_brief()
        canned = ReviewerReport(status="approved", iteration=1, pdf_path=str(pdf))
        fake = _FakeLLM(canned=canned)
        ReviewerAgent().run(
            ReviewInput(brief=brief, ledger=[_ev(brief.entity_id)], estimates=[],
                        pdf_path=str(pdf), iteration=1),
            PipelineContext(llm_client=fake),
        )
        content = fake.captured["messages"][0]["content"]
        text_blocks = [b for b in content if b.get("type") == "text"]
        assert len(text_blocks) >= 2
        cached = text_blocks[0]
        dynamic = text_blocks[1]
        assert cached.get("cache_control") == {"type": "ephemeral"}
        assert "cache_control" not in dynamic
        assert "Evidence Ledger" in cached["text"]
        assert "Rendered PDF Text" in dynamic["text"]
        assert "Author BriefData" in dynamic["text"]
        # The brief JSON must NOT be in the cached prefix
        assert "BriefData" not in cached["text"] or "Author BriefData" not in cached["text"]


class TestLLMClientCacheTokens:
    def test_trace_captures_cache_token_counts(self, tmp_path, monkeypatch):
        """The Anthropic SDK exposes cache_read / cache_creation token counts
        via response.usage. _write_trace must persist them — the Costs page
        will eventually surface them and the regression diff depends on it.
        """
        from account_research.llm_client import LLMClient
        # We patch the SDK client to a stub that returns a faked usage.
        import anthropic

        class _Usage:
            def __init__(self):
                self.input_tokens = 1000
                self.output_tokens = 100
                self.cache_read_input_tokens = 600
                self.cache_creation_input_tokens = 0

        class _Response:
            def __init__(self):
                self.usage = _Usage()
                self.stop_reason = "end_turn"
                self.content = []

        class _Msgs:
            def create(self, **kwargs):
                return _Response()

        class _Client:
            messages = _Msgs()

        client = LLMClient(api_key="sk-test", trace_dir=tmp_path)
        client._client = _Client()  # type: ignore[assignment]
        try:
            client.complete(
                model="claude-opus-4-7",
                system=[{"type": "text", "text": "S",
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": "hi"}],
                agent="unit",
            )
        except Exception:
            pass  # the real call may fail downstream parsing — we only need the trace
        # Read the JSONL line written
        trace_files = list(tmp_path.glob("*.jsonl"))
        assert trace_files, "trace file must be written"
        import json
        records = [json.loads(l) for l in trace_files[0].read_text().splitlines() if l.strip()]
        assert records
        last = records[-1]
        assert last["cache_read_input_tokens"] == 600
        assert last["cache_creation_input_tokens"] == 0
