"""Shared pytest fixtures.

- `session`: a fresh in-memory SQLite session per test, with all tables created.
- `sample_entity`: a persisted Entity row.
- `web_cache_dir`: per-test cache directory for tools.web.direct_web_fetch.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Make sure account_research.db doesn't try to write to a real file before
# we get a chance to override.
os.environ.setdefault("LEDGER_DB_URL", "sqlite:///:memory:")

from account_research.db import Base  # noqa: E402
from account_research.ledger import EntityRow  # noqa: E402 — registers tables with Base
from account_research.schemas.entity import Entity, EntityType  # noqa: E402


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine) -> Session:
    with Session(engine) as s:
        yield s


@pytest.fixture()
def sample_entity(session: Session) -> Entity:
    row = EntityRow(
        id=uuid4(),
        name="BW Project Management",
        type=EntityType.COMPANY.value,
        primary_url="https://bwpm.pro",
        aliases=["BWPM", "bwpm.rd"],
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return Entity(
        id=row.id,
        name=row.name,
        type=EntityType.COMPANY,
        primary_url=row.primary_url,
        aliases=list(row.aliases),
        created_at=row.created_at,
    )


@pytest.fixture()
def web_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "web_cache"
    cache.mkdir()
    monkeypatch.setenv("WEB_CACHE_DIR", str(cache))
    return cache
