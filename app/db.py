"""app/db.py

SQLAlchemy engine/session setup with a neutral default DB URL.

The DATABASE_URL instance parameter (or env var) points at the database;
otherwise a local SQLite file is used so the app boots out of the box.
Nothing here is user-specific.
"""
from __future__ import annotations

import os

from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    """Declarative base for all models."""


def default_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return "sqlite:///./solar_automation.db"


def make_engine(database_url: str | None = None) -> Engine:
    """Create an engine, choosing sane pool args for SQLite vs server DBs."""
    url = database_url or default_database_url()
    if url.startswith("sqlite"):
        kwargs: dict = {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        }
    else:
        kwargs = {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# Default module-level pieces, replaced by alembic/init or tests as needed.
engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


def init_engine(database_url: str | None = None) -> Engine:
    """Set the module-level engine + session factory (called at app startup)."""
    global engine, SessionLocal
    engine = make_engine(database_url)
    SessionLocal = make_session_factory(engine)
    return engine


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, closed afterwards."""
    if SessionLocal is None:
        init_engine()
    assert SessionLocal is not None
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
