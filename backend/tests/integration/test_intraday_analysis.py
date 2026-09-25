"""Days summarised from stored minute bars: whole days measured, the rest only recorded.

Bars and fetches hang off instruments this module creates, on 2025-06-02 —
before any real minute bar or index minute was collected — and index minutes
stored here for that day are removed by date afterwards. The daily KOSPI and
KOSDAQ bars for that day are added only where none exists (a database with
the real ones keeps them untouched), and only what was added is removed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.models import Base, Instrument, MarketIndexBar
from app.models.instrument import Listing
from app.models.intraday import IntradaySummary
from app.repositories import minute_repo
from app.repositories.minute_repo import IndexMinuteRow, MinuteBarRow
from app.services import intraday_service

pytestmark = pytest.mark.integration

SEOUL = ZoneInfo("Asia/Seoul")
KR = MarketCalendar(Market.KR)
DAY = date(2025, 6, 2)


@pytest.fixture(scope="module")
def engine() -> Iterator[object]:
    eng = create_engine(get_settings().database_url, future=True)
    try:
        with eng.connect():
            pass
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


class World:
    def __init__(self, session: Session, ids: list[int]) -> None:
        self.session = session
        self.ids = ids


@pytest.fixture
def world(engine: object) -> Iterator[World]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        # Only the rows this inserts, by the ids the insert returns.
        added_ids = list(
            s.execute(
                pg_insert(MarketIndexBar)
                .values(
                    [
                        {
                            "index_code": code,
                            "ts": KR.session_open(DAY),
                            "available_at": KR.session_close(DAY),
                            "open": Decimal(base),
                            "high": Decimal(base + 20),
                            "low": Decimal(base),
                            "close": Decimal(base + 20),
                        }
                        for code, base in (("^KS11", 2700), ("^KQ11", 750))
                    ]
                )
                .on_conflict_do_nothing(index_elements=["index_code", "ts"])
                .returning(MarketIndexBar.id)
            ).scalars()
        )
        ids = []
        for name, listing in (("쀓분석가", Listing.KOSPI), ("쀓분석나", Listing.KOSDAQ)):
            inst = Instrument(market=Market.KR, name=name, tracked=False, listing=listing)
            s.add(inst)
            s.flush()
            ids.append(inst.instrument_id)
        s.commit()
        try:
            yield World(s, ids)
        finally:
            s.rollback()
            for i in ids:
                s.execute(text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": i})
            s.execute(text("DELETE FROM index_minute_bar WHERE session_date = :d"), {"d": DAY})
            for i in added_ids:
                s.execute(text("DELETE FROM market_index_bar WHERE id = :i"), {"i": i})
            s.commit()


def at(hhmm: str) -> datetime:
    return datetime.combine(DAY, time(int(hhmm[:2]), int(hhmm[2:])), tzinfo=SEOUL)


def minute(instrument_id: int, hhmm: str, o: int, h: int, lo: int, c: int, v: int) -> MinuteBarRow:
    ts = at(hhmm)
    return MinuteBarRow(
        instrument_id, DAY, ts, ts + timedelta(minutes=1), *(Decimal(x) for x in (o, h, lo, c, v))
    )


def store_day(world: World, instrument_id: int, status: str) -> None:
    rows = [
        minute(instrument_id, "0900", 100, 101, 99, 100, 5000),
        minute(instrument_id, "0945", 100, 106, 100, 105, 700),
        minute(instrument_id, "1400", 105, 105, 97, 98, 900),
        minute(instrument_id, "1530", 99, 99, 99, 99, 4000),
    ]
    minute_repo.save_bars(world.session, rows)
    minute_repo.record_fetch(
        world.session, instrument_id=instrument_id, day=DAY, status=status, bars=len(rows), pages=1
    )
    world.session.commit()


def summaries(world: World) -> dict[int, IntradaySummary]:
    world.session.expire_all()
    return {
        s.instrument_id: s
        for s in world.session.execute(
            select(IntradaySummary).where(IntradaySummary.instrument_id.in_(world.ids))
        ).scalars()
    }


def test_a_whole_day_is_measured_and_set_against_its_index(world: World) -> None:
    kospi, _ = world.ids
    store_day(world, kospi, "COMPLETE")
    counts = intraday_service.analyze(world.session, instrument_ids=world.ids)
    assert counts["complete"] == 1
    s = summaries(world)[kospi]
    assert s.status == "COMPLETE" and s.bars == 4
    assert s.return_pct == pytest.approx(-1.0)
    assert (s.mfe_pct, s.mae_pct) == (pytest.approx(6.0), pytest.approx(-3.0))
    assert (s.high_at, s.low_at, s.minutes_to_high) == ("09:45", "14:00", 45)
    assert s.peak_volume_at == "14:00"
    # No index minutes that day: the daily KOSPI bar is the market, and no bucket has one.
    assert (s.index_code, s.market_source) == ("^KS11", "DAILY")
    assert s.market_return_pct is not None
    assert all(b["market_return_pct"] is None for b in s.buckets)
    assert len(s.buckets) == 13


def test_whole_index_minutes_give_the_market_bucket_by_bucket(world: World) -> None:
    kospi, _ = world.ids
    index = []
    for n in range(391):
        # Every minute from 09:00 to 15:29, and the 15:30 line: 391.
        m = 9 * 60 + n
        ts = at(f"{m // 60:02d}{m % 60:02d}")
        price = Decimal(1000 + n)
        index.append(
            IndexMinuteRow("^KS11", DAY, ts, ts + timedelta(minutes=1), price, price, price, price)
        )
    minute_repo.save_index_bars(world.session, index)
    store_day(world, kospi, "COMPLETE")
    intraday_service.analyze(world.session, instrument_ids=world.ids)
    s = summaries(world)[kospi]
    assert s.market_source == "INDEX_MINUTE"
    assert s.market_return_pct == pytest.approx((1390 / 1000 - 1) * 100)
    first = next(b for b in s.buckets if b["start"] == "09:00")
    assert first["market_return_pct"] == pytest.approx((1029 / 1000 - 1) * 100)


def test_a_day_not_whole_is_recorded_without_measures_then_replaced(world: World) -> None:
    kospi, _ = world.ids
    store_day(world, kospi, "PARTIAL")
    counts = intraday_service.analyze(world.session, instrument_ids=world.ids)
    s = summaries(world)[kospi]
    assert (counts["status_only"], s.status, s.return_pct, s.buckets) == (1, "PARTIAL", None, [])
    # Asked again, the same status is left alone.
    assert intraday_service.analyze(world.session, instrument_ids=world.ids)["status_only"] == 0
    # The day arrives whole on a later evening.
    minute_repo.record_fetch(
        world.session, instrument_id=kospi, day=DAY, status="COMPLETE", bars=4, pages=1
    )
    world.session.commit()
    counts = intraday_service.analyze(world.session, instrument_ids=world.ids)
    assert (counts["replaced"], counts["complete"]) == (1, 1)
    assert summaries(world)[kospi].status == "COMPLETE"


def test_a_measured_day_is_not_measured_twice(world: World) -> None:
    kospi, _ = world.ids
    store_day(world, kospi, "COMPLETE")
    intraday_service.analyze(world.session, instrument_ids=world.ids)
    assert intraday_service.analyze(world.session, instrument_ids=world.ids)["complete"] == 0


def test_a_kosdaq_name_is_set_against_kosdaq(world: World) -> None:
    _, kosdaq = world.ids
    store_day(world, kosdaq, "COMPLETE")
    intraday_service.analyze(world.session, instrument_ids=world.ids)
    assert summaries(world)[kosdaq].index_code == "^KQ11"


def test_the_report_reads_the_morning_list_against_whole_days(world: World) -> None:
    from app.models.watchlist import WatchlistMember, WatchlistSnapshot

    kospi, kosdaq = world.ids
    store_day(world, kospi, "COMPLETE")
    intraday_service.analyze(world.session, instrument_ids=world.ids)
    before = intraday_service.report(world.session)
    snap = WatchlistSnapshot(
        session_date=DAY,
        asof=at("0850"),
        strategy_version="PREOPEN_V2",
        selection_version=2,
        versions={},
        inputs={},
        pool=2,
        left_out=0,
    )
    world.session.add(snap)
    world.session.flush()
    for rank, i in enumerate((kospi, kosdaq), 1):
        world.session.add(
            WatchlistMember(
                snapshot_id=snap.id,
                instrument_id=i,
                rank=rank,
                reasons=["TRACKED"],
                tracked=True,
                overlay_events=[],
            )
        )
    world.session.commit()
    try:
        after = intraday_service.report(world.session)
        # One member's day is whole and measured; the other's has no summary yet.
        assert after.members == before.members + 1
        assert after.missing == before.missing + 1
        assert after.watchlist_days == before.watchlist_days + 1
    finally:
        world.session.execute(text("DELETE FROM watchlist_snapshot WHERE id = :i"), {"i": snap.id})
        world.session.commit()


def test_index_minutes_short_of_the_whole_day_fall_back_to_the_daily_bar(world: World) -> None:
    kospi, _ = world.ids
    # Only the afternoon's minutes arrived: not a day to measure buckets against.
    index = []
    for n in range(100):
        m = 13 * 60 + n
        ts = at(f"{m // 60:02d}{m % 60:02d}")
        price = Decimal(2000 + n)
        index.append(
            IndexMinuteRow("^KS11", DAY, ts, ts + timedelta(minutes=1), price, price, price, price)
        )
    minute_repo.save_index_bars(world.session, index)
    store_day(world, kospi, "COMPLETE")
    intraday_service.analyze(world.session, instrument_ids=world.ids)
    s = summaries(world)[kospi]
    assert s.market_source == "DAILY"
    assert all(b["market_return_pct"] is None for b in s.buckets)


def test_a_measured_day_gets_its_market_once_the_index_day_is_on_record(world: World) -> None:
    kospi, _ = world.ids
    store_day(world, kospi, "COMPLETE")
    intraday_service.analyze(world.session, instrument_ids=world.ids)
    # As if the evening's analysis had run before the daily index bar arrived.
    world.session.execute(
        text(
            "UPDATE intraday_summary SET market_source = NULL, market_return_pct = NULL "
            "WHERE instrument_id = :i"
        ),
        {"i": kospi},
    )
    world.session.commit()
    counts = intraday_service.analyze(world.session, instrument_ids=world.ids)
    assert counts["market_filled"] == 1
    s = summaries(world)[kospi]
    assert s.market_source == "DAILY" and s.market_return_pct is not None
    assert s.return_pct == pytest.approx(-1.0)  # the day's own measures untouched


def test_whole_bars_without_an_opening_price_are_settled_not_retried(world: World) -> None:
    kospi, _ = world.ids
    rows = [minute(kospi, "0900", 0, 1, 0, 1, 10), minute(kospi, "0901", 1, 1, 1, 1, 10)]
    minute_repo.save_bars(world.session, rows)
    minute_repo.record_fetch(
        world.session, instrument_id=kospi, day=DAY, status="COMPLETE", bars=2, pages=1
    )
    world.session.commit()
    intraday_service.analyze(world.session, instrument_ids=world.ids)
    assert summaries(world)[kospi].status == "UNMEASURABLE"
    again = intraday_service.analyze(world.session, instrument_ids=world.ids)
    assert (again["replaced"], again["complete"]) == (0, 0)
