"""A run must simulate the period it says it simulated.

Nothing about a period reaching outside the stored data fails on its own. The
engine walks the sessions it was given; the ones with no price behind them
contribute nothing and pass by. What comes back is a result whose every figure
is arithmetically correct and describes a shorter span than the one requested.

Live, before the check existed: 2020-01-02 to 2026-09-18 against two years of
Samsung history returned `sessions = 1651` with a 489-point curve starting in
2024-09-19. A reader would have taken that for a six-year backtest.

Refusing is the harder-to-ignore answer, and it is the one consistent with the
rest of this project: the caller asked about 2020, and "we cannot answer that"
is the truth, while a trimmed window is a different question answered in the
shape of the one asked.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtest.engine import CostModel, Signal
from app.backtest.strategies import BuyAndHold
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.types import Interval
from app.models import Base, Instrument
from app.repositories import candle_repo
from app.repositories.candle_repo import CandleRow
from app.services import backtest_service as svc
from app.services.backtest_service import BacktestWindowError, RunRequest

pytestmark = pytest.mark.integration

US = MarketCalendar(Market.US)

# Data exists for these sessions and no others. Chosen to start on a Monday
# and end on a Friday, so the weekends on either side can be asked for.
HELD = US.sessions_between(date(2025, 3, 3), date(2025, 6, 27))


def _row(iid: int, day: date) -> CandleRow:
    price = Decimal("100")
    return CandleRow(
        instrument_id=iid,
        interval=Interval.DAY_1,
        ts=US.session_open(day),
        available_at=US.session_close(day),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1000"),
        source="TEST",
    )


@pytest.fixture(scope="module")
def db() -> Iterator[object]:
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
def narrow(db: object) -> Iterator[tuple[Session, int]]:
    """An instrument whose history is shorter than people will ask about."""
    factory = sessionmaker(bind=db, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.US, name="WINDOW TEST CORP", us_cik="9999999994")
        s.add(inst)
        s.flush()
        iid = inst.instrument_id

        candle_repo.save_revisions(s, [_row(iid, day) for day in HELD])
        s.commit()

        yield s, iid

        for table in ("candle", "instrument"):
            s.execute(text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": iid})
        s.commit()


def request_for(iid: int, start: date, end: date) -> RunRequest:
    return RunRequest(
        instrument_id=iid,
        start=start,
        end=end,
        starting_cash=Decimal("100000"),
        costs=CostModel(Decimal("0"), Decimal("0")),
    )


class TestPeriodsOutsideTheData:
    def test_a_start_before_the_data_is_refused(self, narrow: tuple[Session, int]) -> None:
        s, iid = narrow
        with pytest.raises(BacktestWindowError, match="data only for"):
            svc.execute(s, BuyAndHold(), request_for(iid, date(2020, 1, 2), HELD[-1]))

    def test_an_end_after_the_data_is_refused(self, narrow: tuple[Session, int]) -> None:
        s, iid = narrow
        with pytest.raises(BacktestWindowError, match="data only for"):
            svc.execute(s, BuyAndHold(), request_for(iid, HELD[0], date(2026, 12, 31)))

    def test_the_refusal_names_what_is_actually_held(self, narrow: tuple[Session, int]) -> None:
        """So the caller can fix the request rather than guess at it."""
        s, iid = narrow
        with pytest.raises(BacktestWindowError) as caught:
            svc.execute(s, BuyAndHold(), request_for(iid, date(2020, 1, 2), HELD[-1]))

        message = str(caught.value)
        assert str(HELD[0]) in message
        assert str(HELD[-1]) in message

    def test_a_period_inside_the_data_runs(self, narrow: tuple[Session, int]) -> None:
        s, iid = narrow
        outcome = svc.execute(s, BuyAndHold(), request_for(iid, HELD[0], HELD[-1]))

        assert outcome.result.sessions == len(HELD)
        assert len(outcome.result.equity_curve) == len(HELD)
        assert outcome.result.simulated_full_period

    def test_the_reported_session_count_matches_the_curve(
        self, narrow: tuple[Session, int]
    ) -> None:
        """The disagreement that made the original defect invisible."""
        s, iid = narrow
        outcome = svc.execute(s, BuyAndHold(), request_for(iid, HELD[0], HELD[-1]))

        assert outcome.result.sessions == len(outcome.result.equity_curve)


class TestWindowsAreJudgedBySessionsNotDates:
    """A window is asked for in ordinary dates; it is run in sessions.

    "2025", "Q2", "through the end of June" — those boundaries land on
    weekends and holidays constantly, and the run only ever touches the
    sessions inside them. Comparing the typed dates against the data's first
    and last session refused questions that were perfectly answerable, and
    walk-forward would have made that the normal case: every window boundary
    is a month, quarter or year end.
    """

    def test_a_weekend_start_before_the_first_session_is_allowed(
        self, narrow: tuple[Session, int]
    ) -> None:
        saturday = date(2025, 3, 1)
        assert not US.is_session(saturday)
        assert saturday < HELD[0]

        s, iid = narrow
        outcome = svc.execute(s, BuyAndHold(), request_for(iid, saturday, HELD[-1]))

        assert outcome.result.sessions == len(HELD)

    def test_a_weekend_end_after_the_last_session_is_allowed(
        self, narrow: tuple[Session, int]
    ) -> None:
        sunday = date(2025, 6, 29)
        assert not US.is_session(sunday)
        assert sunday > HELD[-1]

        s, iid = narrow
        outcome = svc.execute(s, BuyAndHold(), request_for(iid, HELD[0], sunday))

        assert outcome.result.sessions == len(HELD)

    def test_a_genuine_shortfall_is_still_refused(self, narrow: tuple[Session, int]) -> None:
        """Normalising to sessions must not soften the real check."""
        s, iid = narrow
        earlier = US.sessions_between(date(2025, 2, 20), date(2025, 2, 28))[0]
        assert earlier < HELD[0]

        with pytest.raises(BacktestWindowError, match="data only for"):
            svc.execute(s, BuyAndHold(), request_for(iid, earlier, HELD[-1]))

    def test_a_window_containing_no_session_is_refused(self, narrow: tuple[Session, int]) -> None:
        """A weekend on its own is not a backtest period."""
        s, iid = narrow
        with pytest.raises(BacktestWindowError, match="trading sessions between"):
            svc.execute(s, BuyAndHold(), request_for(iid, date(2025, 6, 28), date(2025, 6, 29)))


class TestGapsInsideTheData:
    def test_a_hole_in_the_middle_is_refused_too(self, narrow: tuple[Session, int]) -> None:
        """Coverage is judged on outer dates, so a gap gets past that check.

        It still means the run covered less than it claims, so the same
        refusal applies — caught here from the engine's own count rather than
        from the coverage bounds.
        """
        s, iid = narrow
        middle = HELD[len(HELD) // 2]
        s.execute(
            text("DELETE FROM candle WHERE instrument_id = :i AND ts >= :a AND ts < :b"),
            {
                "i": iid,
                "a": US.session_open(middle),
                "b": US.session_open(middle) + timedelta(days=20),
            },
        )
        s.commit()

        # The outer dates still have data, so the coverage check passes.
        with pytest.raises(BacktestWindowError, match="produced no bar"):
            svc.execute(s, BuyAndHold(), request_for(iid, HELD[0], HELD[-1]))

    def test_the_gap_can_be_accepted_deliberately(self, narrow: tuple[Session, int]) -> None:
        """A one-day halt is normal; a missed collection is not, and only the
        caller can tell them apart. Overriding is allowed and stays visible."""
        s, iid = narrow
        middle = HELD[len(HELD) // 2]
        s.execute(
            text("DELETE FROM candle WHERE instrument_id = :i AND ts >= :a AND ts < :b"),
            {
                "i": iid,
                "a": US.session_open(middle),
                "b": US.session_open(middle) + timedelta(days=20),
            },
        )
        s.commit()

        outcome = svc.execute(
            s,
            BuyAndHold(),
            request_for(iid, HELD[0], HELD[-1]),
            require_complete_sessions=False,
        )
        assert outcome.result.sessions_without_data
        assert not outcome.result.simulated_full_period

    def test_a_stale_marked_session_still_appears_in_the_curve(
        self, narrow: tuple[Session, int]
    ) -> None:
        """The portfolio is worth something on a day it did not trade, and
        dropping the day would shorten the curve rather than mark it."""
        s, iid = narrow
        middle = HELD[len(HELD) // 2]
        s.execute(
            text("DELETE FROM candle WHERE instrument_id = :i AND ts = :a"),
            {"i": iid, "a": US.session_open(middle)},
        )
        s.commit()

        outcome = svc.execute(
            s,
            BuyAndHold(),
            request_for(iid, HELD[0], HELD[-1]),
            require_complete_sessions=False,
        )
        assert outcome.result.sessions_without_data == [middle]
        assert middle in [p.day for p in outcome.result.equity_curve]
        assert len(outcome.result.equity_curve) == len(HELD)


class TestFillsStayInsideTheWindow:
    def test_a_final_session_entry_does_not_fill_beyond_the_end(
        self, narrow: tuple[Session, int]
    ) -> None:
        """Against a real database, where the next session's price exists."""
        s, iid = narrow
        cut = HELD[len(HELD) // 2]
        last_close = US.session_close(cut)

        class EnterAtTheEnd:
            def evaluate(self, data: object, instrument_id: int) -> Signal:
                return Signal.ENTER if data.asof == last_close else Signal.HOLD  # type: ignore[attr-defined]

        outcome = svc.execute(s, EnterAtTheEnd(), request_for(iid, HELD[0], cut))

        assert outcome.result.fills == []
        assert outcome.result.unfilled
        assert "outside the backtest window" in outcome.result.unfilled[0].reason

    def test_the_next_session_really_does_have_a_price(self, narrow: tuple[Session, int]) -> None:
        """Otherwise the test above proves nothing about the window."""
        s, iid = narrow
        cut = HELD[len(HELD) // 2]
        after = HELD[len(HELD) // 2 + 1]

        assert candle_repo.opening_price(s, iid, Interval.DAY_1, US.session_open(after)) == Decimal(
            "100"
        )
        assert after > cut
