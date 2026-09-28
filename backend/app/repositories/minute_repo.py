"""Minute bars: stored once per minute, with a record of whether each day came in whole."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.intraday import IndexMinuteBar, LiveSecondBar, MinuteBar, MinuteFetch
from app.repositories import bulk

COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
EMPTY = "EMPTY"
# The provider answered with an error for this day; asked again next run.
ERROR = "ERROR"
# A day in one of these states is settled and not asked for again.
SETTLED = (COMPLETE, EMPTY)


class MinuteBarRow(NamedTuple):
    instrument_id: int
    session_date: date
    ts: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class IndexMinuteRow(NamedTuple):
    index_code: str
    session_date: date
    ts: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class SecondBarRow(NamedTuple):
    instrument_id: int
    session_date: date
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def save_second_bars(session: Session, rows: Sequence[SecondBarRow]) -> int:
    """실시간 1초봉을 넣거나 고친다. commit하지 않는다. 넣거나 고친 행 수를 돌려준다.

    저장하는 순간 진행 중이던 초는 체결이 더 들어와 다음 저장에서 다시 오므로, 겹치면 새 값으로 갱신한다.
    한 문장 안에 같은 (종목, 초)가 두 번 있으면 Postgres가 거절하니 호출하는 쪽이 중복을 없앤다.
    """
    written = 0
    for batch in bulk.batched(rows, columns=len(SecondBarRow._fields)):
        stmt = pg_insert(LiveSecondBar).values([r._asdict() for r in batch])
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "ts"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        written += len(session.execute(stmt.returning(LiveSecondBar.id)).scalars().all())
    return written


def save_bars(session: Session, rows: Sequence[MinuteBarRow]) -> int:
    """Insert, ignoring minutes already stored. Does not commit. Returns rows new."""
    written = 0
    for batch in bulk.batched(rows, columns=len(MinuteBarRow._fields)):
        stmt = pg_insert(MinuteBar).values([r._asdict() for r in batch])
        stmt = stmt.on_conflict_do_nothing(index_elements=["instrument_id", "ts"])
        written += len(session.execute(stmt.returning(MinuteBar.id)).scalars().all())
    return written


def save_index_bars(session: Session, rows: Sequence[IndexMinuteRow]) -> int:
    written = 0
    for batch in bulk.batched(rows, columns=len(IndexMinuteRow._fields)):
        stmt = pg_insert(IndexMinuteBar).values([r._asdict() for r in batch])
        stmt = stmt.on_conflict_do_nothing(index_elements=["index_code", "ts"])
        written += len(session.execute(stmt.returning(IndexMinuteBar.id)).scalars().all())
    return written


def record_fetch(
    session: Session, *, instrument_id: int, day: date, status: str, bars: int, pages: int
) -> None:
    session.add(
        MinuteFetch(
            instrument_id=instrument_id, session_date=day, status=status, bars=bars, pages=pages
        )
    )
    session.flush()


def latest_status(
    session: Session, *, instrument_ids: Collection[int], days: Collection[date]
) -> dict[tuple[int, date], str]:
    """The newest fetch status of each (instrument, day) asked about that has one."""
    if not instrument_ids or not days:
        return {}
    f = MinuteFetch
    stmt = (
        select(f.instrument_id, f.session_date, f.status)
        .distinct(f.instrument_id, f.session_date)
        .where(f.instrument_id.in_(list(instrument_ids)), f.session_date.in_(list(days)))
        .order_by(f.instrument_id, f.session_date, f.fetched_at.desc(), f.id.desc())
    )
    return {(i, d): s for i, d, s in session.execute(stmt).all()}


def bars_for(session: Session, instrument_id: int, day: date) -> list[MinuteBar]:
    b = MinuteBar
    stmt = select(b).where(b.instrument_id == instrument_id, b.session_date == day).order_by(b.ts)
    return list(session.execute(stmt).scalars())


def index_bars_for(session: Session, index_code: str, day: date) -> list[IndexMinuteBar]:
    b = IndexMinuteBar
    stmt = select(b).where(b.index_code == index_code, b.session_date == day).order_by(b.ts)
    return list(session.execute(stmt).scalars())


def index_minutes(session: Session, index_code: str, day: date) -> int:
    b = IndexMinuteBar
    return int(
        session.execute(
            select(func.count()).where(b.index_code == index_code, b.session_date == day)
        ).scalar_one()
    )


def latest_statuses(session: Session) -> dict[tuple[int, date], tuple[str, int]]:
    """The newest fetch status and bar count of every (instrument, day) ever fetched."""
    f = MinuteFetch
    stmt = (
        select(f.instrument_id, f.session_date, f.status, f.bars)
        .distinct(f.instrument_id, f.session_date)
        .order_by(f.instrument_id, f.session_date, f.fetched_at.desc(), f.id.desc())
    )
    return {(i, d): (s, n) for i, d, s, n in session.execute(stmt).all()}
