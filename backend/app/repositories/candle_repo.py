"""Candle persistence and retrieval.

Writes go through a Postgres `ON CONFLICT DO UPDATE` so that re-collecting a
range is idempotent. That is not a nicety: backfill and the nightly job overlap
by design, and a provider occasionally restates a bar after the close.

`ingested_at` is set on insert and deliberately **not** refreshed on conflict.
It records when we first obtained the row, which is the transaction-time axis a
reproducible backtest filters on. Bumping it on every re-collection would make
old data look newly arrived and quietly break reproduce mode.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Candle, Interval


class CandleRow(TypedDict):
    """One bar ready for persistence."""

    instrument_id: int
    interval: Interval
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str


def upsert_many(session: Session, rows: Sequence[CandleRow]) -> int:
    """Insert or update bars. Returns the number of rows sent.

    Uses a single statement rather than per-row merges; a year of daily bars
    across a watchlist is thousands of rows and round-tripping each one is the
    difference between a second and a minute.
    """
    if not rows:
        return 0

    stmt = pg_insert(Candle).values(list(rows))
    stmt = stmt.on_conflict_do_update(
        index_elements=[Candle.instrument_id, Candle.interval, Candle.ts],
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "volume": stmt.excluded.volume,
            "source": stmt.excluded.source,
            # ingested_at intentionally omitted: first-seen time must not move.
        },
    )
    session.execute(stmt)
    return len(rows)


def latest_ts(session: Session, instrument_id: int, interval: Interval) -> datetime | None:
    """Timestamp of the newest stored bar, used to decide backfill ranges."""
    stmt = (
        select(Candle.ts)
        .where(Candle.instrument_id == instrument_id, Candle.interval == interval)
        .order_by(Candle.ts.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def history(
    session: Session,
    instrument_id: int,
    interval: Interval,
    *,
    limit: int = 250,
    until: datetime | None = None,
) -> list[Candle]:
    """Most recent bars, oldest first.

    `until` bounds the query by bar timestamp for ordinary display use. It is
    **not** a point-in-time filter — backtests must go through
    `app.backtest.pit_repository`, which also applies the `ingested_at` bound.
    """
    stmt = select(Candle).where(
        Candle.instrument_id == instrument_id,
        Candle.interval == interval,
    )
    if until is not None:
        stmt = stmt.where(Candle.ts <= until)

    stmt = stmt.order_by(Candle.ts.desc()).limit(limit)
    return list(reversed(session.execute(stmt).scalars().all()))


def count_for(session: Session, instrument_id: int, interval: Interval) -> int:
    stmt = select(Candle).where(Candle.instrument_id == instrument_id, Candle.interval == interval)
    return len(session.execute(stmt).scalars().all())
