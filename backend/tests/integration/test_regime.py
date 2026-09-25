"""Index bars stored once, read as of a moment, and the regime filed beside a signal.

The index is a test code (^TEST…) so nothing here reads or writes the real
KOSPI, KOSDAQ or S&P bars; every bar is removed by that prefix afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.types import MissingFactorPolicy, SignalAction
from app.models import Base, Instrument, Signal, SignalRegime
from app.models.instrument import Listing
from app.repositories import market_index_repo
from app.repositories.market_index_repo import IndexBarRow
from app.scoring.regime import DEFAULT, Label
from app.services import regime_service

pytestmark = pytest.mark.integration

KR = MarketCalendar(Market.KR)
SESSIONS = KR.sessions_between(date(2025, 1, 2), date(2026, 9, 23))[-(DEFAULT.history_needed + 5) :]
CODE = "^TEST1"


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


def bar(day: date, close: float, code: str = CODE) -> IndexBarRow:
    c = Decimal(str(round(close, 6)))
    return IndexBarRow(
        index_code=code,
        ts=KR.session_open(day),
        available_at=KR.session_close(day),
        open=c,
        high=c,
        low=c,
        close=c,
    )


def rising_and_calm() -> list[IndexBarRow]:
    n = len(SESSIONS)
    return [
        bar(day, 100 * 1.002**i * (1 + (0.02 * (1 - i / n) + 0.001) * (-1) ** i))
        for i, day in enumerate(SESSIONS)
    ]


class World:
    def __init__(self, session: Session, instrument: Instrument) -> None:
        self.session = session
        self.instrument = instrument


@pytest.fixture
def world(engine: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    monkeypatch.setattr(regime_service, "index_for", lambda *_: CODE)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.KR, name="쀓국면", tracked=False)
        s.add(inst)
        s.commit()
        try:
            yield World(s, inst)
        finally:
            s.rollback()
            s.execute(text("DELETE FROM market_index_bar WHERE index_code LIKE '^TEST%'"))
            s.execute(
                text("DELETE FROM signal WHERE instrument_id = :i"), {"i": inst.instrument_id}
            )
            s.execute(
                text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": inst.instrument_id}
            )
            s.commit()


def signal_at(world: World, day: date) -> Signal:
    row = Signal(
        instrument_id=world.instrument.instrument_id,
        data_asof=KR.session_close(day),
        decision_at=KR.session_close(day),
        earliest_execution_at=KR.next_session_open(day),
        total_score=55.0,
        action=SignalAction.WATCH,
        policy=MissingFactorPolicy.ZERO,
        reasons=[],
        strategy_version="regime-test",
    )
    world.session.add(row)
    world.session.commit()
    return row


class TestBars:
    def test_a_session_is_stored_once(self, world: World) -> None:
        rows = rising_and_calm()[:3]
        assert market_index_repo.save_bars(world.session, rows) == 3
        assert market_index_repo.save_bars(world.session, rows) == 0

    def test_a_bar_is_read_only_once_its_session_has_closed(self, world: World) -> None:
        day, next_day = SESSIONS[-2], SESSIONS[-1]
        market_index_repo.save_bars(world.session, [bar(day, 100.0), bar(next_day, 105.0)])
        at_close = KR.session_close(next_day)
        assert market_index_repo.closes_asof(
            world.session, CODE, asof=at_close - timedelta(minutes=1), limit=5
        ) == [100.0]
        assert market_index_repo.closes_asof(world.session, CODE, asof=at_close, limit=5) == [
            100.0,
            105.0,
        ]

    def test_another_index_is_not_mixed_in(self, world: World) -> None:
        market_index_repo.save_bars(
            world.session, [bar(SESSIONS[-1], 100.0), bar(SESSIONS[-1], 999.0, code="^TEST2")]
        )
        asof = KR.session_close(SESSIONS[-1])
        assert market_index_repo.closes_asof(world.session, CODE, asof=asof, limit=5) == [100.0]


class TestBesideTheSignal:
    def test_the_regime_is_filed_and_the_signal_is_untouched(self, world: World) -> None:
        market_index_repo.save_bars(world.session, rising_and_calm())
        row = signal_at(world, SESSIONS[-1])

        regime = regime_service.attach(world.session, row)

        assert regime is not None
        assert (regime.label, regime.index_code, regime.regime_version) == (
            Label.RISK_ON,
            CODE,
            DEFAULT.version,
        )
        assert regime.asof == row.decision_at
        world.session.expire_all()
        stored = world.session.get(Signal, row.id)
        assert stored is not None
        assert (stored.total_score, stored.action) == (55.0, SignalAction.WATCH)

    def test_the_next_sessions_close_is_not_used(self, world: World) -> None:
        bars = rising_and_calm()
        market_index_repo.save_bars(world.session, bars)
        row = signal_at(world, SESSIONS[-2])
        regime = regime_service.attach(world.session, row)
        assert regime is not None
        assert regime.index_close == pytest.approx(float(bars[-2].close))

    def test_without_history_the_regime_is_unknown(self, world: World) -> None:
        market_index_repo.save_bars(world.session, rising_and_calm()[-10:])
        regime = regime_service.attach(world.session, signal_at(world, SESSIONS[-1]))
        assert regime is not None and regime.label == Label.UNKNOWN

    def test_without_the_days_index_bar_nothing_is_filed_until_it_arrives(
        self, world: World
    ) -> None:
        bars = rising_and_calm()
        market_index_repo.save_bars(world.session, bars[:-1])
        row = signal_at(world, SESSIONS[-1])

        # Yesterday's close is there; filing it as today's would pass for today.
        assert regime_service.attach(world.session, row) is None
        assert regime_service.backfill(world.session, signal_ids=[row.id]) == 0

        market_index_repo.save_bars(world.session, bars[-1:])
        world.session.commit()
        assert regime_service.backfill(world.session, signal_ids=[row.id]) == 1
        filed = world.session.execute(
            select(SignalRegime).where(SignalRegime.signal_id == row.id)
        ).scalar_one()
        assert filed.index_close == pytest.approx(float(bars[-1].close))

    def test_a_failing_regime_costs_the_signal_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = signal_at(world, SESSIONS[-1])

        def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("regime broke")

        monkeypatch.setattr(regime_service, "regime_at", broken)
        assert regime_service.attach(world.session, row) is None
        world.session.expire_all()
        assert world.session.get(Signal, row.id) is not None

    def test_backfill_files_only_what_is_missing(self, world: World) -> None:
        market_index_repo.save_bars(world.session, rising_and_calm())
        first = signal_at(world, SESSIONS[-2])
        second = signal_at(world, SESSIONS[-1])
        regime_service.attach(world.session, first)

        ids = [first.id, second.id]
        assert regime_service.backfill(world.session, signal_ids=ids) == 1
        assert regime_service.backfill(world.session, signal_ids=ids) == 0
        filed = world.session.execute(
            select(SignalRegime.signal_id).where(SignalRegime.signal_id.in_(ids))
        ).scalars()
        assert sorted(filed) == sorted(ids)


def test_the_real_boards_are_the_ones_collected() -> None:
    from app.collectors.market_index import INDEXES

    for market, listing in ((Market.KR, None), (Market.US, None)):
        assert regime_service.index_for(market, listing) in INDEXES
    assert regime_service.index_for(Market.KR, Listing.KOSDAQ) in INDEXES
