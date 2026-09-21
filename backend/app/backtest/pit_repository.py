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

**Reading and filling are different questions.** `bars` returns only completed
bars, because a strategy must not see a close that has not happened.
`opening_price` returns one price from a bar that is still open, because an
opening price is knowable the moment it prints and a fill at the next
session's open is a real trade. Collapsing the two leaves no correct answer: a
Friday-open fill either cannot be simulated, or is simulated by reading
Friday's finished bar and taking its `open` — the right number from a row that
did not exist yet.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.core.types import Bar
from app.models import Interval
from app.models.fundamental import FundamentalSource
from app.repositories import candle_repo, fundamental_repo
from app.repositories.fundamental_repo import (
    FactLookup,
    FundamentalContext,
    RevisionPolicy,
)


class PitViolationError(Exception):
    """A read was attempted outside the window the run is allowed to see."""


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

    def opening_price(self, instrument_id: int, interval: Interval) -> Decimal | None:
        """The price a fill at *this reader's instant* would get, or None.

        Separate from `bars` on purpose, and the separation is the point.

        `bars` answers what the strategy may *read*, so it returns only bars
        that had completed — at 10:00 the day's close does not exist. Fills
        need something else. A daily decision taken at Thursday's close fills
        at Friday's open, and Friday's bar does not complete until Friday's
        close, so asking `bars` for it returns nothing and the trade cannot be
        simulated at all. Waiting for the bar to complete and then reading its
        `open` gets the right number out of a row that did not exist yet.

        An opening price is knowable at the instant it prints, which is what
        makes this safe where reading the whole bar would not be. It returns a
        single `Decimal` so a caller cannot reach past it to `.close`.

        **It takes no timestamp.** An earlier version accepted the execution
        instant as an argument, and that argument was never checked against
        the simulation clock: standing at Thursday's close, a caller could ask
        for Friday's open and get it, because only `ingested_at` was bound.
        Transaction time held while simulation time was simply bypassed — in
        the one method built to be the door. Reading the clock the reader
        already carries makes the wrong question unaskable, which is the whole
        reason this is a class. The engine positions the reader at the fill
        instant (`reader.at(execution_at).opening_price(...)`) and the
        position is then the only thing it can be asked about.
        """
        return candle_repo.opening_price(
            self._session,
            instrument_id,
            interval,
            self.asof,
            ingested_before=self._snapshot,
        )

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
