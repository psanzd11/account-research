"""Database engine and session factory.

Defaults to SQLite at <repo_root>/outputs/ledger.db. Override with the
LEDGER_DB_URL environment variable (e.g. for Postgres in production).
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "outputs" / "ledger.db"


def _default_url() -> str:
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"


DB_URL = os.environ.get("LEDGER_DB_URL", _default_url())

engine = create_engine(DB_URL, future=True)


# SQLite default journal mode serializes writers — concurrent processes
# crash with "database is locked". WAL allows multiple readers + one writer
# with a busy_timeout so writers wait their turn instead of failing.
# This is a no-op for non-SQLite URLs (e.g. Postgres in prod).
@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):
    if not DB_URL.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")  # 15s wait if locked
        cursor.execute("PRAGMA synchronous=NORMAL")  # safe with WAL
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Create all tables. Idempotent."""
    # Import here so models register with Base.metadata before create_all.
    from account_research import ledger  # noqa: F401

    Base.metadata.create_all(bind=engine)
