"""Database engine, session factory and the advisory-lock helper.

Nothing outside `app.repositories` should import this module. The import-linter
contract in `.importlinter` enforces that: factor engines and the backtest
engine are forbidden from reaching SQLAlchemy directly, because the
point-in-time filter lives in the repository layer and a query that bypasses it
silently reintroduces look-ahead bias.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from zlib import crc32

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide SQLAlchemy engine."""
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    with get_session_factory()() as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for worker jobs and scripts."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _lock_key(name: str) -> int:
    """Map a job name to a signed 64-bit advisory-lock key."""
    # crc32 gives 32 unsigned bits; shift into the signed 64-bit range Postgres
    # expects while keeping collisions as unlikely as the hash allows.
    return crc32(name.encode("utf-8")) - 2**31


@contextmanager
def advisory_lock(session: Session, name: str) -> Iterator[bool]:
    """Try to hold a Postgres advisory lock named `name` for the block.

    Yields True if the lock was acquired, False if another process holds it.
    Scheduled jobs use this so that accidentally running two workers cannot
    double-fire a collector: the second one simply declines to run.

    The lock is session-scoped and released on exit, including on error.
    """
    key = _lock_key(name)
    acquired = bool(session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
    try:
        yield acquired
    finally:
        if acquired:
            session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            session.commit()
