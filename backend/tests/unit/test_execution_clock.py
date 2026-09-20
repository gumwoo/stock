"""When a decision is allowed to become a fill.

Point-in-time filtering governs what a strategy may read. This governs when it
may act, and the second is the easier one to get wrong because breaking it
produces no missing data and no exception — only a slightly better number.

The case that matters: a signal computed from a session's close cannot fill at
that same close. The close does not exist until the session is over.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.backtest.execution import (
    ExecutionDecision,
    ExecutionModel,
    ExecutionTimingError,
    assert_executable,
    decide,
    earliest_execution,
)
from app.core.calendar import Market, MarketCalendar

US = MarketCalendar(Market.US)
KR = MarketCalendar(Market.KR)


def close_of(cal: MarketCalendar, day: str) -> datetime:
    return cal.session_close(date.fromisoformat(day))


def open_of(cal: MarketCalendar, day: str) -> datetime:
    return cal.session_open(date.fromisoformat(day))


class TestTheCloseIsNotTradable:
    def test_a_close_decision_does_not_fill_at_that_close(self) -> None:
        """The whole point. Allowing it is look-ahead wearing a different hat."""
        decision = close_of(US, "2025-11-20")
        assert earliest_execution(US, decision) > decision

    def test_it_fills_at_the_next_session_open(self) -> None:
        decision = close_of(US, "2025-11-20")
        assert earliest_execution(US, decision) == open_of(US, "2025-11-21")

    def test_a_same_close_fill_raises_rather_than_being_nudged(self) -> None:
        """Correcting it silently would let a strategy violate this all run."""
        decision = close_of(US, "2025-11-20")
        with pytest.raises(ExecutionTimingError, match="precedes the earliest tradable"):
            assert_executable(US, decision_at=decision, execution_at=decision)

    def test_a_fill_before_the_decision_raises(self) -> None:
        with pytest.raises(ExecutionTimingError):
            assert_executable(
                US,
                decision_at=close_of(US, "2025-11-20"),
                execution_at=open_of(US, "2025-11-20"),
            )


class TestNonTradingDays:
    """Holidays are not special cases here — the calendar answers them."""

    def test_friday_close_fills_monday(self) -> None:
        assert earliest_execution(US, close_of(US, "2025-11-21")) == open_of(US, "2025-11-24")

    def test_a_decision_before_thanksgiving_skips_the_holiday(self) -> None:
        """Wednesday's close, with Thursday closed, fills on Friday."""
        assert earliest_execution(US, close_of(US, "2025-11-26")) == open_of(US, "2025-11-28")

    def test_an_early_close_is_still_a_close(self) -> None:
        """Christmas Eve shuts at 13:00 ET; the fill is the 26th regardless."""
        eve = close_of(US, "2025-12-24")
        assert eve.hour == 18  # 13:00 ET
        assert earliest_execution(US, eve) == open_of(US, "2025-12-26")

    def test_korean_new_year_closure(self) -> None:
        """KRX shut for six days over Seollal 2025 — Jan 25 to 30 inclusive,
        the public holiday plus a temporary closure on the Monday. A Friday
        close therefore fills the following Friday, and nothing here knows
        that; the calendar does."""
        fill = earliest_execution(KR, close_of(KR, "2025-01-24"))
        assert fill == open_of(KR, "2025-01-31")
        assert (fill - close_of(KR, "2025-01-24")).days == 6


class TestIntradayBars:
    def test_the_next_bar_is_the_fill(self) -> None:
        decision = datetime(2025, 11, 20, 15, 0, tzinfo=UTC)  # 10:00 ET
        fill = earliest_execution(US, decision, model=ExecutionModel.NEXT_BAR, bar_minutes=30)
        assert fill == datetime(2025, 11, 20, 15, 30, tzinfo=UTC)

    def test_the_last_bar_of_a_session_rolls_to_the_next_open(self) -> None:
        """Otherwise a 15:30 ET decision fills at 16:00 ET, after the bell."""
        decision = close_of(US, "2025-11-20").replace(hour=20, minute=30)
        fill = earliest_execution(US, decision, model=ExecutionModel.NEXT_BAR, bar_minutes=30)
        assert fill == open_of(US, "2025-11-21")

    def test_next_bar_without_a_bar_length_refuses_to_guess(self) -> None:
        with pytest.raises(ValueError, match="bar_minutes"):
            earliest_execution(US, close_of(US, "2025-11-20"), model=ExecutionModel.NEXT_BAR)


class TestDecisionsDeriveTheirOwnFill:
    def test_decide_returns_a_consistent_triple(self) -> None:
        data_asof = close_of(US, "2025-11-20")
        result = decide(US, data_asof=data_asof, decision_at=data_asof)

        assert result.data_asof == data_asof
        assert result.execution_at == open_of(US, "2025-11-21")

    def test_a_decision_cannot_predate_its_own_data(self) -> None:
        with pytest.raises(ExecutionTimingError, match="precedes its own data"):
            ExecutionDecision(
                data_asof=close_of(US, "2025-11-20"),
                decision_at=open_of(US, "2025-11-20"),
                execution_at=open_of(US, "2025-11-21"),
                model=ExecutionModel.NEXT_OPEN,
            )

    def test_same_close_is_not_an_expressible_model(self) -> None:
        """The one model that cannot be made correct is absent, not discouraged."""
        assert [m.value for m in ExecutionModel] == ["NEXT_OPEN", "NEXT_BAR"]

    def test_naive_datetimes_are_refused_at_the_boundary(self) -> None:
        with pytest.raises(ValueError, match="decision_at"):
            earliest_execution(US, datetime(2025, 11, 20, 21, 0))
