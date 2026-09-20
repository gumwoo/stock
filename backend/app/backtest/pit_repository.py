"""Point-in-time data access — the only door into the backtest's data.

Two filters, always applied together:

    available_at <= simulation_asof     could the market have known this?
    ingested_at  <= data_snapshot_at    did we have it when the run happened?

The second exists because a later backfill carries a *past* `filed_at` and
therefore sails through the first filter. A backtest run before the backfill
and re-run after it would produce different numbers from the same inputs, and
nothing in the first filter can detect that.

Why this is a class rather than a module of functions: the two instants must
travel together. A helper that takes them as optional arguments can be called
with one of them omitted, and the result looks entirely normal — slightly
better, usually. `PitReader` is constructed once per run with both bound, and
there is no way to ask it a question that skips either.

It is also the only place in `app.backtest` permitted to hold a session. CI
forbids `engine`, `metrics`, `walkforward` and `execution` from importing
SQLAlchemy at all, so a strategy that wants data has no route but this one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.models import Interval
from app.models.fundamental import FundamentalSource
from app.repositories import candle_repo, fundamental_repo
from app.repositories.fundamental_repo import (
    FactLookup,
    FundamentalContext,
    RevisionPolicy,
)

# How far back to look when resolving a named bar. Generous enough for a fill
# a few sessions after its decision, bounded so a wrong timestamp cannot turn
# into a full-history scan.
_FILL_SEARCH_DEPTH = 40


class PitViolationError(Exception):
    """A read was attempted outside the window the run is allowed to see."""


@dataclass(frozen=True, slots=True)
class Bar:
    """A completed bar, detached from the ORM.

    Plain values, so the simulation cannot accidentally hold a live ORM object
    whose lazy loads would reach the database outside the PIT filter.
    """

    ts: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class PitReader:
    """Reads data as it stood at one simulation instant, under one snapshot.

    `asof` moves as the simulation walks forward; `data_snapshot_at` is fixed
    for the whole run and is what makes a re-run reproducible.
    """

    __slots__ = ("_asof", "_session", "_snapshot")

    def __init__(
        self,
        session: Session,
        *,
        data_snapshot_at: datetime,
        asof: datetime | None = None,
    ) -> None:
        self._session = session
        self._snapshot = ensure_utc(data_snapshot_at, field="data_snapshot_at")
        self._asof = ensure_utc(asof, field="asof") if asof is not None else None

    @property
    def asof(self) -> datetime:
        if self._asof is None:
            raise PitViolationError("this reader has no simulation instant; call at() first")
        return self._asof

    @property
    def data_snapshot_at(self) -> datetime:
        return self._snapshot

    def at(self, asof: datetime) -> PitReader:
        """A reader positioned at a new simulation instant, same snapshot.

        Returns a new reader rather than mutating this one, so a value held
        across a step cannot silently start answering for a later moment.
        """
        moment = ensure_utc(asof, field="asof")
        if moment > self._snapshot:
            # Simulating past the snapshot is not a point-in-time question any
            # more: beyond it the two filters stop agreeing about what exists,
            # and the run would no longer be the thing it claims to reproduce.
            raise PitViolationError(
                f"simulation instant {moment.isoformat()} is after the data snapshot "
                f"{self._snapshot.isoformat()}; the run would read data it did not have"
            )
        return PitReader(self._session, data_snapshot_at=self._snapshot, asof=moment)

    # --- prices -----------------------------------------------------------

    def bars(self, instrument_id: int, interval: Interval, *, limit: int = 250) -> list[Bar]:
        """Completed bars, oldest first, that were both knowable and held.

        Bound by `available_at`, not by bar start. At 10:00 the day's bar has
        opened but its close does not exist, and returning it would hand the
        strategy a price from its own future.
        """
        rows = candle_repo.history(
            self._session,
            instrument_id,
            interval,
            limit=limit,
            available_before=self.asof,
            ingested_before=self._snapshot,
        )
        return [
            Bar(
                ts=r.ts,
                available_at=r.available_at,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
            )
            for r in rows
        ]

    def bar_at(self, instrument_id: int, interval: Interval, ts: datetime) -> Bar | None:
        """One specific bar, if it was knowable and held.

        Used for fills: the engine names the instant it wants to trade at and
        gets the bar or nothing. Nothing means the fill cannot happen, which is
        the correct answer for a suspended or not-yet-complete session.
        """
        moment = ensure_utc(ts, field="ts")
        for bar in self.bars(instrument_id, interval, limit=_FILL_SEARCH_DEPTH):
            if bar.ts == moment:
                return bar
        return None

    # --- fundamentals -----------------------------------------------------

    def fact(
        self,
        instrument_id: int,
        context: FundamentalContext,
        *,
        source: FundamentalSource | None = None,
        policy: RevisionPolicy = RevisionPolicy.AS_KNOWN_THEN,
    ) -> FactLookup:
        """One fundamental fact as it was known then, with its absence reason.

        The revision policy is part of the strategy, not of the plumbing: the
        figure first reported and the figure as later restated are different
        inputs, and a strategy must say which one it trades on.
        """
        return fundamental_repo.value_as_of(
            self._session,
            instrument_id,
            context,
            asof=self.asof,
            policy=policy,
            ingested_before=self._snapshot,
            source=source,
        )


def snapshot_now(session: Session) -> datetime:
    """The instant to record as a run's `data_snapshot_at`.

    Taken from the database rather than the process, so a clock skew between
    the worker and Postgres cannot place the snapshot before rows the database
    has already stamped as ingested.
    """
    result = session.execute(select(func.now())).scalar_one()
    return ensure_utc(result, field="data_snapshot_at")


def coverage(
    session: Session,
    instrument_id: int,
    interval: Interval,
    *,
    data_snapshot_at: datetime,
) -> tuple[date, date] | None:
    """First and last bar date available under a snapshot, or None.

    A run whose requested period reaches outside this range would silently
    simulate on nothing for part of it and report the result as a full-period
    return.
    """
    rows = candle_repo.history(
        session,
        instrument_id,
        interval,
        limit=1_000_000,
        ingested_before=ensure_utc(data_snapshot_at, field="data_snapshot_at"),
    )
    if not rows:
        return None
    return rows[0].ts.date(), rows[-1].ts.date()
