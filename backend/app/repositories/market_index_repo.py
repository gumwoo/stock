"""Market index bars: stored once per session, read as they stood at a moment."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.models.market import MarketIndexBar
from app.repositories import bulk


class IndexBarRow(NamedTuple):
    index_code: str
    ts: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def save_bars(session: Session, rows: Sequence[IndexBarRow]) -> int:
    """Insert, ignoring sessions already stored. Does not commit. Returns rows new."""
    written = 0
    for batch in bulk.batched(rows, columns=len(IndexBarRow._fields)):
        stmt = pg_insert(MarketIndexBar).values([r._asdict() for r in batch])
        stmt = stmt.on_conflict_do_nothing(index_elements=["index_code", "ts"])
        written += len(session.execute(stmt.returning(MarketIndexBar.id)).scalars().all())
    return written


def latest_available(session: Session, index_code: str, *, asof: datetime) -> datetime | None:
    """When the newest bar complete by `asof` completed."""
    b = MarketIndexBar
    stmt = select(func.max(b.available_at)).where(
        b.index_code == index_code, b.available_at <= ensure_utc(asof, field="asof")
    )
    return session.execute(stmt).scalar_one_or_none()


def closes_asof(session: Session, index_code: str, *, asof: datetime, limit: int) -> list[float]:
    """The last `limit` closes complete by `asof`, oldest first."""
    b = MarketIndexBar
    stmt = (
        select(b.close)
        .where(b.index_code == index_code, b.available_at <= ensure_utc(asof, field="asof"))
        .order_by(b.ts.desc())
        .limit(limit)
    )
    return [float(c) for c in reversed(session.execute(stmt).scalars().all())]
