"""Candle persistence and retrieval.

Bars are **append-only**. Re-collecting an unchanged bar writes nothing;
a bar whose values have changed writes a new revision beside the old one.

Nothing is ever updated in place. That rule is not fastidiousness — an in-place
update while holding `ingested_at` at its original value creates a row whose
values arrived on one date and whose transaction time claims another, and a
reproduce-mode query filtering `ingested_at <= data_snapshot_at` would then
serve a correction into a snapshot taken before it existed. Keeping revisions
is the same choice `fundamental` makes for filings, for the same reason.

Reads take the newest revision. Point-in-time reads take the newest revision
that had arrived by the snapshot instant.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import TypedDict

from sqlalchemy import Numeric, and_, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement, Subquery

from app.models import Candle, Interval
from app.repositories import bulk

# Columns that define a bar's content. A change in any of them is a genuine
# restatement and earns a new revision; `source` and `ingested_at` do not.
#
# `available_at` is included deliberately. It is derived from the exchange
# calendar, so a correction to it — an early close that a fixed-offset backfill
# got wrong, say — is a change in what we claim about the bar and deserves to
# be recorded as a revision rather than silently skipped.
_VALUE_COLUMNS = ("available_at", "open", "high", "low", "close", "volume")


class CandleRow(TypedDict):
    """One bar ready for persistence."""

    instrument_id: int
    interval: Interval
    ts: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str


def _latest_revisions(
    session: Session,
    instrument_id: int,
    interval: Interval,
    timestamps: Sequence[datetime],
) -> dict[datetime, Candle]:
    """Newest stored revision for each of `timestamps`."""
    if not timestamps:
        return {}

    rows = (
        session.execute(
            select(Candle)
            .where(
                Candle.instrument_id == instrument_id,
                Candle.interval == interval,
                Candle.ts.in_(timestamps),
            )
            .order_by(Candle.ts, Candle.ingested_at)
        )
        .scalars()
        .all()
    )

    # Ordered ascending by ingested_at, so the last write per ts wins.
    latest: dict[datetime, Candle] = {}
    for row in rows:
        latest[row.ts] = row
    return latest


def _unchanged(existing: Candle, incoming: CandleRow) -> bool:
    return all(
        getattr(existing, column) == incoming[column]  # type: ignore[literal-required]
        for column in _VALUE_COLUMNS
    )


def save_revisions(session: Session, rows: Sequence[CandleRow]) -> int:
    """Store bars, skipping those that are unchanged. Returns rows written.

    A bar seen for the first time is inserted. A bar whose values match the
    newest stored revision is ignored. A bar whose values differ is inserted as
    a new revision, leaving the previous one intact.
    """
    if not rows:
        return 0

    by_key: dict[tuple[int, Interval], list[CandleRow]] = {}
    for row in rows:
        by_key.setdefault((row["instrument_id"], row["interval"]), []).append(row)

    to_insert: list[CandleRow] = []
    for (instrument_id, interval), group in by_key.items():
        latest = _latest_revisions(session, instrument_id, interval, [r["ts"] for r in group])
        for row in group:
            existing = latest.get(row["ts"])
            if existing is not None and _unchanged(existing, row):
                continue
            to_insert.append(row)

    if not to_insert:
        return 0

    # Ten years of daily bars for one instrument is well under the ceiling; a
    # backfill across the whole watchlist in one call is not. See `bulk`.
    for batch in bulk.batched(to_insert, columns=len(CandleRow.__annotations__)):
        session.execute(pg_insert(Candle).values(list(batch)))
    return len(to_insert)


def latest_ts(session: Session, instrument_id: int, interval: Interval) -> datetime | None:
    """Timestamp of the newest stored bar, used to decide backfill ranges."""
    stmt = select(func.max(Candle.ts)).where(
        Candle.instrument_id == instrument_id, Candle.interval == interval
    )
    return session.execute(stmt).scalar()


def _finite() -> ColumnElement[bool]:
    """시가·고가·저가·종가가 모두 NaN이 아닌 행.

    Postgres numeric의 NaN은 NaN끼리 같다고 본다. 그래서 `!=`로 걸러진다. NaN 수정본은 없는 것으로
    보고, 그 앞의 정상 수정본이 그 봉을 대신한다(J&J 2026-08-25: 9/22 273.14 → 9/25 NaN).
    """
    nan = literal("NaN").cast(Numeric)
    return and_(Candle.open != nan, Candle.high != nan, Candle.low != nan, Candle.close != nan)


def _newest_revision_subquery(
    instrument_id: int,
    interval: Interval,
    *,
    ingested_before: datetime | None = None,
) -> Subquery:
    """Per-timestamp maximum `ingested_at`, optionally bounded by a snapshot."""
    stmt = select(Candle.ts, func.max(Candle.ingested_at).label("ingested_at")).where(
        Candle.instrument_id == instrument_id,
        Candle.interval == interval,
        _finite(),
    )
    if ingested_before is not None:
        stmt = stmt.where(Candle.ingested_at <= ingested_before)
    return stmt.group_by(Candle.ts).subquery()


def history(
    session: Session,
    instrument_id: int,
    interval: Interval,
    *,
    limit: int = 250,
    since: datetime | None = None,
    until: datetime | None = None,
    available_before: datetime | None = None,
    ingested_before: datetime | None = None,
) -> list[Candle]:
    """Newest revision of each bar, oldest first.

    Args:
        since: earliest bar *start* to return. Not a point-in-time filter —
            it narrows which history a caller may learn from, which is what
            makes a rolling training window actually roll.
        until: bound by bar *start*, for ordinary display. Not a
            point-in-time filter — a bar that has opened is not yet knowable.
        available_before: bound by bar *completion*. This is the honest
            simulation filter: at 10:00 the day's bar has opened but its close
            does not exist yet, so it must not be returned.
        ingested_before: bound by arrival time, so a caller can reconstruct
            what was stored at a past instant.

    Backtests should still go through `app.backtest.pit_repository`, which
    applies the last two together as one enforced rule.
    """
    newest = _newest_revision_subquery(instrument_id, interval, ingested_before=ingested_before)

    stmt = (
        select(Candle)
        .join(
            newest,
            (Candle.ts == newest.c.ts) & (Candle.ingested_at == newest.c.ingested_at),
        )
        .where(Candle.instrument_id == instrument_id, Candle.interval == interval)
    )
    if since is not None:
        stmt = stmt.where(Candle.ts >= since)
    if until is not None:
        stmt = stmt.where(Candle.ts <= until)
    if available_before is not None:
        stmt = stmt.where(Candle.available_at <= available_before)

    stmt = stmt.order_by(Candle.ts.desc()).limit(limit)
    return list(reversed(session.execute(stmt).scalars().all()))


def revisions_of(
    session: Session, instrument_id: int, interval: Interval, ts: datetime
) -> list[Candle]:
    """Every stored revision of one bar, oldest first.

    Exists so a restatement is inspectable rather than merely survived.
    """
    stmt = (
        select(Candle)
        .where(
            Candle.instrument_id == instrument_id,
            Candle.interval == interval,
            Candle.ts == ts,
        )
        .order_by(Candle.ingested_at)
    )
    return list(session.execute(stmt).scalars().all())


def count_for(session: Session, instrument_id: int, interval: Interval) -> int:
    """Number of distinct bars, counting a restated bar once."""
    stmt = select(func.count(func.distinct(Candle.ts))).where(
        Candle.instrument_id == instrument_id, Candle.interval == interval, _finite()
    )
    return int(session.execute(stmt).scalar() or 0)


def opening_price(
    session: Session,
    instrument_id: int,
    interval: Interval,
    ts: datetime,
    *,
    ingested_before: datetime | None = None,
) -> Decimal | None:
    """The open of the bar beginning exactly at `ts`, or None.

    Deliberately not filtered by `available_at`, and deliberately returning one
    number rather than a bar.

    A bar's `available_at` is when it *completes*, which is the right gate for
    a strategy: at 10:00 the day's close does not exist. But an opening price
    is known at 10:00, because that is when it printed. A fill at the open of
    the next session is a real trade a real person could place, and gating it
    on bar completion would make it impossible to simulate — leaving only the
    alternative of reading the finished bar afterwards and taking its `open`,
    which gets the same number by way of a row that did not exist yet.

    Returning `Decimal` rather than the row is what keeps that distinction from
    eroding. A caller holding the bar could reach for `.close`, and nothing
    would look wrong.
    """
    stmt = (
        select(Candle.open)
        .where(
            Candle.instrument_id == instrument_id,
            Candle.interval == interval,
            Candle.ts == ts,
            _finite(),
        )
        .order_by(Candle.ingested_at.desc())
        .limit(1)
    )
    if ingested_before is not None:
        stmt = stmt.where(Candle.ingested_at <= ingested_before)
    return session.execute(stmt).scalars().first()
