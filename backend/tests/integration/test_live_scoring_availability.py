"""The live scorer obeys the same availability rule as the backtest.

`candle_repo.history` grew an `available_before` filter for point-in-time
reads, but the live path was still calling it without one. That gap mattered:
yfinance serves the day's partial bar during trading hours, so mid-session the
scorer could pick up an OHLCV that was still forming and stamp the resulting
signal with a `data_asof` in the future.

The deeper problem is that it would make the two paths disagree. A backtest
applying the filter and a live scorer skipping it are no longer running the
same strategy, which quietly undoes the reason for sharing engine code at all.

Two guards are tested here: the repository refuses to return an unfinished bar,
and the scorer raises if one somehow reaches it anyway.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.models import Base, Instrument, Interval, SymbolHistory
from app.repositories import candle_repo
from app.repositories.candle_repo import CandleRow
from app.services import scoring_service

pytestmark = pytest.mark.integration

KR = MarketCalendar(Market.KR)
SESSION_DAY = datetime(2026, 9, 18, tzinfo=UTC).date()


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


@pytest.fixture
def instrument(engine: object) -> Iterator[tuple[Session, Instrument]]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.KR, name="LIVE TEST CO")
        s.add(inst)
        s.flush()
        s.add(
            SymbolHistory(
                instrument_id=inst.instrument_id,
                symbol="LIVE",
                valid_from=datetime(2020, 1, 1, tzinfo=UTC).date(),
                source="SEED",
            )
        )
        s.commit()
        yield s, inst
        for table in ("signal_factor", "signal", "candle", "symbol_history", "instrument"):
            column = "instrument_id" if table != "signal_factor" else None
            if column:
                s.execute(
                    text(f"DELETE FROM {table} WHERE instrument_id = :i"),
                    {"i": inst.instrument_id},
                )
        s.commit()


def seed_sessions(session: Session, instrument_id: int, count: int) -> None:
    """Store `count` completed daily bars ending on SESSION_DAY."""
    rows: list[CandleRow] = []
    day = SESSION_DAY
    for i in range(count):
        while not KR.is_session(day):
            day -= timedelta(days=1)
        opened = KR.session_open(day)
        price = Decimal(str(100 + i))
        rows.append(
            CandleRow(
                instrument_id=instrument_id,
                interval=Interval.DAY_1,
                ts=opened,
                available_at=KR.bar_available_at(opened),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal("1000"),
                source="TEST",
            )
        )
        day -= timedelta(days=1)
    candle_repo.save_revisions(session, rows)
    session.commit()


class TestRepositoryGuard:
    def test_an_open_bar_is_withheld_mid_session(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        session, inst = instrument
        seed_sessions(session, inst.instrument_id, 3)

        mid_session = KR.session_open(SESSION_DAY) + timedelta(hours=3)

        bars = candle_repo.history(
            session, inst.instrument_id, Interval.DAY_1, available_before=mid_session
        )

        assert all(b.available_at <= mid_session for b in bars)
        assert KR.session_open(SESSION_DAY) not in [b.ts for b in bars]

    def test_the_same_bar_is_served_after_the_close(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        session, inst = instrument
        seed_sessions(session, inst.instrument_id, 3)

        after_close = KR.bar_available_at(KR.session_open(SESSION_DAY))

        bars = candle_repo.history(
            session, inst.instrument_id, Interval.DAY_1, available_before=after_close
        )

        assert KR.session_open(SESSION_DAY) in [b.ts for b in bars]


class TestScorerNeverLooksForward:
    def test_data_asof_is_never_in_the_future(self, instrument: tuple[Session, Instrument]) -> None:
        """The property that matters, stated directly."""
        session, inst = instrument
        seed_sessions(session, inst.instrument_id, 80)

        mid_session = KR.session_open(SESSION_DAY) + timedelta(hours=3)
        signal = scoring_service.score_instrument(session, inst, now=mid_session, peers=None)

        assert signal is not None
        assert signal.data_asof <= mid_session, "scored from a bar that had not closed"
        assert signal.decision_at <= mid_session

    def test_mid_session_scoring_uses_the_previous_close(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        session, inst = instrument
        seed_sessions(session, inst.instrument_id, 80)

        mid_session = KR.session_open(SESSION_DAY) + timedelta(hours=3)
        signal = scoring_service.score_instrument(session, inst, now=mid_session, peers=None)

        assert signal is not None
        assert signal.data_asof < KR.session_open(SESSION_DAY)

    def test_after_the_close_it_uses_todays_bar(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        session, inst = instrument
        seed_sessions(session, inst.instrument_id, 80)

        after_close = KR.bar_available_at(KR.session_open(SESSION_DAY)) + timedelta(minutes=5)
        signal = scoring_service.score_instrument(session, inst, now=after_close, peers=None)

        assert signal is not None
        assert signal.data_asof == KR.bar_available_at(KR.session_open(SESSION_DAY))

    def test_execution_is_still_pushed_to_the_next_session(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        session, inst = instrument
        seed_sessions(session, inst.instrument_id, 80)

        after_close = KR.bar_available_at(KR.session_open(SESSION_DAY)) + timedelta(minutes=5)
        signal = scoring_service.score_instrument(session, inst, now=after_close, peers=None)

        assert signal is not None
        assert signal.earliest_execution_at > signal.decision_at
        assert signal.earliest_execution_at == KR.next_session_open(SESSION_DAY)
