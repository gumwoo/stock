"""Issued KIS credentials: the newest still valid, and when the last one was issued."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.db import _lock_key
from app.models.kis import KisCredential


def lock_issue(session: Session, kind: str) -> None:
    """Serialise issuing across processes until this transaction ends."""
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(f"kis_issue_{kind}")})


def usable(session: Session, *, env: str, kind: str, until: datetime) -> KisCredential | None:
    """The newest credential of this kind that is still valid at `until`."""
    c = KisCredential
    stmt = (
        select(c)
        .where(c.env == env, c.kind == kind, c.expires_at > ensure_utc(until, field="until"))
        .order_by(c.issued_at.desc(), c.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def last_issued(session: Session, *, env: str, kind: str) -> datetime | None:
    c = KisCredential
    return session.execute(
        select(func.max(c.issued_at)).where(c.env == env, c.kind == kind)
    ).scalar_one_or_none()


def save(session: Session, *, env: str, kind: str, value: str, expires_at: datetime) -> None:
    """Store an issued credential. Does not commit."""
    session.add(
        KisCredential(
            env=env, kind=kind, value=value, expires_at=ensure_utc(expires_at, field="expires_at")
        )
    )
    session.flush()


def expire(session: Session, *, env: str, kind: str, value: str) -> int:
    """Mark the stored credential with this value as lapsed now. Does not commit."""
    c = KisCredential
    rows = (
        session.execute(
            select(c).where(
                c.env == env, c.kind == kind, c.value == value, c.expires_at > func.now()
            )
        )
        .scalars()
        .all()
    )
    now = db_now(session)
    for row in rows:
        row.expires_at = now
    session.flush()
    return len(rows)


def db_now(session: Session) -> datetime:
    result: datetime = session.execute(select(func.clock_timestamp())).scalar_one()
    return result
