"""Bars that fall outside the loaded trading calendar.

`--period max` asks yfinance for everything it has, and for Apple that reaches
1980. The exchange calendars are loaded from 1990, because a filing register
going back to the mid-1990s is the furthest anything here needs, and loading
two further decades of XKRX costs seconds per process to store bars no backtest
reaches.

A daily bar's `available_at` is its session's close. Below the calendar's first
session there is no session to ask about, so the bar cannot be given an honest
availability — and the calendar wrapper raises rather than guessing one, which
is the right call and also what took the whole collector down: one instrument
listed in 1980 ended the run before any of the other seventeen were reached.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from app.collectors.yfinance_history import YFinanceHistoryCollector
from app.core.calendar import Market, MarketCalendar

NOW = datetime(2026, 9, 21, tzinfo=UTC)
US = MarketCalendar(Market.US)


def frame(days: list[date]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [10.0] * len(days),
            "High": [11.0] * len(days),
            "Low": [9.0] * len(days),
            "Close": [10.5] * len(days),
            "Volume": [1000.0] * len(days),
        },
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]),
    )


def rows(days: list[date]) -> tuple[list[object], int]:
    produced, skipped = YFinanceHistoryCollector._to_rows(
        frame(days), instrument_id=1, calendar=US, now=NOW
    )
    return list(produced), skipped


class TestBarsBelowTheCalendar:
    def test_they_are_skipped_rather_than_raising(self) -> None:
        """The behaviour that matters: one 1980 bar must not end the run."""
        produced, skipped = rows([date(1980, 12, 12), date(2020, 1, 2)])

        assert skipped == 1
        assert len(produced) == 1

    def test_they_are_counted_so_the_collector_can_say_so(self) -> None:
        early = [date(1985, 1, 2), date(1986, 1, 2), date(1987, 1, 2)]

        _, skipped = rows([*early, date(2020, 1, 2)])

        assert skipped == len(early)

    def test_the_bar_kept_is_the_one_inside_the_calendar(self) -> None:
        produced, _ = rows([date(1980, 12, 12), date(2020, 1, 2)])

        assert produced[0]["ts"] == US.session_open(date(2020, 1, 2))  # type: ignore[index]

    def test_nothing_is_skipped_when_everything_is_in_range(self) -> None:
        _, skipped = rows([date(2020, 1, 2), date(2020, 1, 3)])

        assert skipped == 0

    def test_the_first_session_itself_is_kept(self) -> None:
        """The boundary is inclusive; the calendar can answer for its own
        first session."""
        produced, skipped = rows([US.first_session])

        assert skipped == 0
        assert len(produced) == 1

    def test_a_bar_below_the_calendar_is_not_confused_with_a_holiday(self) -> None:
        """Both are skipped, and only one of them is worth reporting.

        A non-session day inside the calendar is yfinance being loose with its
        index and says nothing about coverage. A day below the calendar means
        history we chose not to load, which is a fact about this system.
        """
        christmas = date(2020, 12, 25)
        assert not US.is_session(christmas)

        produced, skipped = rows([christmas, date(2020, 1, 2)])

        assert skipped == 0
        assert len(produced) == 1


class TestTheRestOfTheConversionIsUnchanged:
    def test_an_unfinished_session_is_still_skipped(self) -> None:
        """A bar whose session has not closed must never reach the scorer."""
        produced, _ = YFinanceHistoryCollector._to_rows(
            frame([date(2020, 1, 2)]),
            instrument_id=1,
            calendar=US,
            now=US.session_open(date(2020, 1, 2)),
        )

        assert list(produced) == []

    @pytest.mark.parametrize("day", [date(2020, 1, 2), date(2020, 6, 1)])
    def test_a_bar_is_anchored_to_its_session_open(self, day: date) -> None:
        produced, _ = rows([day])

        assert produced[0]["ts"] == US.session_open(day)  # type: ignore[index]
        assert produced[0]["available_at"] == US.bar_available_at(  # type: ignore[index]
            US.session_open(day)
        )
