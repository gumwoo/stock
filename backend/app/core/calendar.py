"""Trading-session calendar.

A thin wrapper over `exchange_calendars` (XKRX for KRX, XNYS for US). We do not
maintain holiday data ourselves — that library already tracks it and keeping our
own copy would mean owning a maintenance burden with no upside.

Two rules in this system depend on getting sessions right:

1. `available_at` for filings. DART gives `rcept_dt` as a date only, and SEC's
   `filed` is a date too, so we cannot tell a 06:00 dissemination from a 14:00
   one. The conservative, defensible rule is: usable from the *next* session's
   open. `next_session_open()` implements that.

2. Execution timing. A signal decided on the close of session D may not fill on
   session D — the close is not knowable until the session ends.
   `next_tradable_open()` gives the earliest honest fill time.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import StrEnum
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

from app.core.clock import ensure_utc


class Market(StrEnum):
    """Markets this system trades. Values match the Toss API's market codes."""

    KR = "KR"
    US = "US"


_CALENDAR_CODE: dict[Market, str] = {
    Market.KR: "XKRX",
    Market.US: "XNYS",
}


# `exchange_calendars` defaults to roughly the last twenty years, which is
# not enough here: SEC's filing register reaches back to the 1990s, and
# resolving a 1994 filing's availability needs a session that far back. Loading
# from 1990 costs about 0.2s for XNYS and 2.5s for XKRX, once per process.
CALENDAR_START = "1990-01-01"


@lru_cache(maxsize=4)
def _calendar(market: Market) -> xcals.ExchangeCalendar:
    """Load and cache the underlying calendar. Construction is expensive."""
    return xcals.get_calendar(_CALENDAR_CODE[market], start=CALENDAR_START)


class MarketCalendar:
    """Session queries for one market. All returned datetimes are UTC."""

    def __init__(self, market: Market) -> None:
        self.market = market
        self._cal = _calendar(market)

    def is_session(self, day: date) -> bool:
        """True if `day` is a trading session (not a weekend or holiday)."""
        return bool(self._cal.is_session(pd.Timestamp(day)))

    def session_open(self, day: date) -> datetime:
        """Opening instant of the session on `day`.

        Raises:
            ValueError: if `day` is not a trading session.
        """
        ts = pd.Timestamp(day)
        if not self._cal.is_session(ts):
            raise ValueError(f"{day} is not a {self.market} trading session")
        return ensure_utc(self._cal.session_open(ts).to_pydatetime(), field="session_open")

    def local_today(self, now: datetime) -> date:
        """`now` as a date in the exchange's own timezone.

        Asked in UTC instead, a KRX session closing at 06:30 UTC would belong
        to the previous UTC date for most of the trading day.
        """
        return ensure_utc(now, field="now").astimezone(self._cal.tz).date()

    def has_closed(self, now: datetime) -> bool:
        """Did this market open today, in its own timezone, and has it finished?

        One question rather than two, because a scheduled job wants a single
        answer: a holiday and a session still in progress both mean "not yet",
        and neither is a reason to collect.

        The job itself is fired by a cron in the market's own timezone, which
        is what keeps a fixed UTC time from drifting an hour twice a year — NYSE
        closes at 21:00 UTC in winter and 20:00 in summer. This method is the
        check behind that trigger rather than a replacement for it: it refuses
        holidays, and it refuses an early firing on a half day.
        """
        moment = ensure_utc(now, field="now")
        today = self.local_today(moment)
        if not self.is_session(today):
            return False
        return moment >= self.session_close(today)

    def session_close(self, day: date) -> datetime:
        """Closing instant of the session on `day`.

        Raises:
            ValueError: if `day` is not a trading session.
        """
        ts = pd.Timestamp(day)
        if not self._cal.is_session(ts):
            raise ValueError(f"{day} is not a {self.market} trading session")
        return ensure_utc(self._cal.session_close(ts).to_pydatetime(), field="session_close")

    @property
    def first_session(self) -> date:
        """Earliest session this calendar knows about."""
        result: date = self._cal.first_session.date()
        return result

    @property
    def last_session(self) -> date:
        """Latest session this calendar knows about, roughly a year ahead."""
        result: date = self._cal.last_session.date()
        return result

    def covers(self, day: date) -> bool:
        """Whether a session can be found after `day` at all.

        Asked before trusting a date from outside the process. Past the ends
        the answer is an exception — our own `ValueError` below the start, the
        library's `DateOutOfBounds` above the end — and a collector that
        listed those one by one would miss the next, which is how the same
        defect kept reappearing in this codebase. A question with a yes-or-no
        answer cannot be half-guarded.
        """
        return self.first_session <= day < self.last_session

    def next_session(self, day: date) -> date:
        """The first trading session strictly after `day`.

        `day` need not itself be a session — a filing can land on a Saturday.

        Raises:
            ValueError: if `day` predates the calendar's range. Deliberately
                not clamped: silently snapping a 1970 date to 1990 would make
                an availability timestamp quietly wrong.
        """
        if day < self.first_session:
            raise ValueError(
                f"{day} is before the {self.market} calendar begins "
                f"({self.first_session}); widen CALENDAR_START rather than "
                "guessing a session"
            )
        # Walk to the first session on or after `day`, then step once more if
        # that landed on `day` itself, since we need strictly after.
        following = self._cal.date_to_session(pd.Timestamp(day), direction="next")
        if following.date() <= day:
            following = self._cal.next_session(following)
        result: date = following.date()
        return result

    def session_on_or_after(self, day: date) -> date:
        """`day` itself if it is a session, else the next session after it."""
        result: date = self._cal.date_to_session(pd.Timestamp(day), direction="next").date()
        return result

    def next_session_open(self, day: date) -> datetime:
        """Open of the first session strictly after `day`.

        This is the `available_at` rule for date-granular filings: a disclosure
        dated `day` is treated as usable only from the following session's open.
        """
        return self.session_open(self.next_session(day))

    def bar_available_at(self, ts: datetime, *, minutes: int | None = None) -> datetime:
        """When a bar starting at `ts` is complete, and therefore knowable.

        A bar's close, high, low and volume do not exist until it ends, so the
        instant it becomes usable is its end, never its start. For a daily bar
        that is the session close; for an intraday bar it is `ts` plus the bar
        length.

        Args:
            minutes: bar length for intraday bars. Omit for daily bars.
        """
        moment = ensure_utc(ts, field="ts")
        if minutes is not None:
            return moment + timedelta(minutes=minutes)
        return self.session_close(moment.date())

    def sessions_between(self, start: date, end: date) -> list[date]:
        """Every trading session in `[start, end]`, inclusive, in order.

        A backtest walks these rather than calendar days. Iterating days and
        skipping non-sessions gives the same list, but also silently gives an
        empty simulation when a period is wrong, whereas an empty list here is
        visible to the caller.
        """
        if end < start:
            raise ValueError(f"end {end} precedes start {start}")
        sessions = self._cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
        return [ts.date() for ts in sessions]

    def is_open_at(self, ts: datetime) -> bool:
        """True if the market is in session at this instant.

        The close is excluded. A bar ending exactly at the close does not open
        a further bar in that session, and treating it as if it did would
        invent a trading opportunity that never existed.
        """
        moment = ensure_utc(ts, field="ts")
        day = moment.date()
        if not self.is_session(day):
            return False
        return self.session_open(day) <= moment < self.session_close(day)

    def next_tradable_open(self, after: datetime) -> datetime:
        """Earliest session open strictly after the instant `after`.

        The execution-timing invariant: a decision made at `after` cannot fill
        before this moment.
        """
        moment = ensure_utc(after, field="after")
        candidate = self.session_on_or_after(moment.date())
        open_at = self.session_open(candidate)
        if open_at > moment:
            return open_at
        return self.next_session_open(candidate)
