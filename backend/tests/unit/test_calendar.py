"""Trading-session invariants.

Two rules in this system rest entirely on these functions:

* the `available_at` rule for date-granular filings (usable from the *next*
  session open, because a date cannot distinguish a 06:00 dissemination from a
  14:00 one), and
* the execution-timing rule (a decision made on a close cannot fill on that
  same close).

Both are wrong in the same direction if the calendar is wrong — they would let
the system see the future — so they get explicit coverage including weekends
and holidays.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.calendar import Market, MarketCalendar


@pytest.fixture
def kr() -> MarketCalendar:
    return MarketCalendar(Market.KR)


@pytest.fixture
def us() -> MarketCalendar:
    return MarketCalendar(Market.US)


class TestSessionIdentification:
    def test_weekday_is_a_session(self, kr: MarketCalendar) -> None:
        assert kr.is_session(date(2026, 9, 18))  # Friday

    def test_weekend_is_not_a_session(self, kr: MarketCalendar) -> None:
        assert not kr.is_session(date(2026, 9, 19))  # Saturday
        assert not kr.is_session(date(2026, 9, 20))  # Sunday

    def test_session_open_rejects_a_non_session(self, kr: MarketCalendar) -> None:
        with pytest.raises(ValueError, match="not a KR trading session"):
            kr.session_open(date(2026, 9, 19))


class TestSessionTimes:
    def test_kr_opens_at_0900_kst(self, kr: MarketCalendar) -> None:
        # 09:00 KST == 00:00 UTC
        assert kr.session_open(date(2026, 9, 18)) == datetime(2026, 9, 18, 0, 0, tzinfo=UTC)

    def test_us_opens_at_0930_et(self, us: MarketCalendar) -> None:
        # 09:30 EDT == 13:30 UTC
        assert us.session_open(date(2026, 9, 18)) == datetime(2026, 9, 18, 13, 30, tzinfo=UTC)

    def test_session_times_are_utc(self, kr: MarketCalendar) -> None:
        assert kr.session_close(date(2026, 9, 18)).utcoffset() == timedelta(0)


class TestNextSession:
    def test_friday_rolls_to_monday(self, kr: MarketCalendar) -> None:
        assert kr.next_session(date(2026, 9, 18)) == date(2026, 9, 21)

    def test_saturday_rolls_to_monday(self, kr: MarketCalendar) -> None:
        """A filing can land on a weekend; the argument need not be a session."""
        assert kr.next_session(date(2026, 9, 19)) == date(2026, 9, 21)

    def test_next_session_is_strictly_after(self, kr: MarketCalendar) -> None:
        """Given a session, it must advance rather than return the same day.

        This is the whole point for `available_at`: a disclosure dated D must
        not be usable during session D.
        """
        assert kr.next_session(date(2026, 9, 18)) > date(2026, 9, 18)

    def test_session_on_or_after_does_not_advance_a_session(self, kr: MarketCalendar) -> None:
        assert kr.session_on_or_after(date(2026, 9, 18)) == date(2026, 9, 18)


class TestAvailableAtRule:
    def test_filing_is_not_usable_on_its_own_date(self, kr: MarketCalendar) -> None:
        """The core PIT rule for date-granular disclosures.

        DART gives rcept_dt as YYYYMMDD and SEC gives `filed` as a date. Neither
        tells us whether the document appeared before or after the open, so the
        conservative boundary is the next session's open.
        """
        filed_on = date(2026, 9, 18)  # Friday

        available_at = kr.next_session_open(filed_on)

        assert available_at == datetime(2026, 9, 21, 0, 0, tzinfo=UTC)  # Monday open
        assert available_at > kr.session_close(filed_on)

    def test_weekend_filing_waits_for_monday(self, us: MarketCalendar) -> None:
        assert us.next_session_open(date(2026, 9, 19)) == datetime(2026, 9, 21, 13, 30, tzinfo=UTC)


class TestExecutionTiming:
    def test_decision_after_close_fills_next_session(self, kr: MarketCalendar) -> None:
        """A signal decided on Friday's close cannot fill on Friday."""
        friday_close = kr.session_close(date(2026, 9, 18))

        earliest = kr.next_tradable_open(friday_close)

        assert earliest == datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
        assert earliest > friday_close

    def test_decision_before_open_fills_at_that_open(self, us: MarketCalendar) -> None:
        """Pre-market decisions may fill at the upcoming open, not the one after."""
        premarket = datetime(2026, 9, 18, 11, 0, tzinfo=UTC)  # 07:00 ET

        assert us.next_tradable_open(premarket) == datetime(2026, 9, 18, 13, 30, tzinfo=UTC)

    def test_intraday_decision_waits_for_the_next_open(self, us: MarketCalendar) -> None:
        """Mid-session decisions cannot fill at an open that already happened."""
        midday = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)  # 12:00 ET

        earliest = us.next_tradable_open(midday)

        assert earliest == datetime(2026, 9, 21, 13, 30, tzinfo=UTC)
        assert earliest > midday

    def test_rejects_naive_input(self, kr: MarketCalendar) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            kr.next_tradable_open(datetime(2026, 9, 18, 6, 30))


class TestHolidays:
    def test_kr_new_year_is_not_a_session(self, kr: MarketCalendar) -> None:
        assert not kr.is_session(date(2026, 1, 1))

    def test_us_independence_day_observed(self, us: MarketCalendar) -> None:
        # 2026-07-04 is a Saturday, observed Friday 2026-07-03.
        assert not us.is_session(date(2026, 7, 3))

    def test_holiday_pushes_availability_further(self, us: MarketCalendar) -> None:
        """A filing before a long weekend waits for the market to reopen."""
        available = us.next_session_open(date(2026, 7, 2))
        assert available.date() == date(2026, 7, 6)  # Monday after the observed holiday
