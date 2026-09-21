"""Execution-timing invariants.

Point-in-time filtering answers *what the strategy could read*. It says nothing
about *when the strategy could act*, and the second is just as easy to get
wrong. A signal computed from a session's close cannot fill at that same close:
the close does not exist until the session is over. Permitting it is look-ahead
bias wearing a different hat, and it is the more dangerous of the two because
it produces no missing data and no exception — only a slightly better number.

Three instants, kept distinct:

    data_asof     the timestamp of the data the decision was computed from
    decision_at   when the decision became possible, i.e. when that data was
                  complete and readable
    execution_at  when the resulting order could actually fill

The invariant is `execution_at >= next_tradable_open(decision_at)`. It raises
rather than corrects. Silently pushing a bad fill forward would mean a strategy
could violate the rule all day and still produce a plausible equity curve —
which is the failure mode this project keeps finding, a wrong number that looks
entirely normal.

The module is pure: it takes a calendar and instants and returns instants. It
holds no session and reads no table, which CI enforces.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.core.calendar import MarketCalendar
from app.core.clock import ensure_utc


class ExecutionModel(StrEnum):
    """How a decision is turned into a fill time.

    `SAME_CLOSE` is deliberately absent rather than merely discouraged. It is
    the one model that cannot be made correct, so it is not expressible.
    """

    NEXT_OPEN = "NEXT_OPEN"
    """Fill at the open of the first session that begins after the decision.

    The honest default for a daily strategy: the decision is made on a
    completed session, and the earliest a human could act on it is the next
    time the market opens.
    """

    NEXT_BAR = "NEXT_BAR"
    """Fill at the start of the next bar of the strategy's own interval.

    For intraday strategies, where the next tradable instant is the following
    bar rather than the following session. On a 30-minute clock a decision
    taken when the 09:30 bar completes fills at 10:00 — the same instant,
    because that is when the next bar opens.
    """


class ExecutionTimingError(Exception):
    """A fill was claimed at or before the moment its decision became possible."""


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    """A decision and the earliest instant it could be acted on."""

    data_asof: datetime
    decision_at: datetime
    execution_at: datetime
    model: ExecutionModel

    def __post_init__(self) -> None:
        if self.decision_at < self.data_asof:
            raise ExecutionTimingError(
                f"decision_at {self.decision_at.isoformat()} precedes its own data "
                f"({self.data_asof.isoformat()}); the decision was made before the "
                "data it used was readable"
            )
        if self.execution_at < self.decision_at:
            raise ExecutionTimingError(
                f"execution_at {self.execution_at.isoformat()} precedes "
                f"decision_at {self.decision_at.isoformat()}"
            )
        if self.execution_at == self.decision_at and self.model is not ExecutionModel.NEXT_BAR:
            # Under NEXT_OPEN the two instants are genuinely apart: a close is
            # hours from the next open. Anywhere else, equality means a fill at
            # the close that produced the decision.
            raise ExecutionTimingError(
                f"execution_at {self.execution_at.isoformat()} is not after "
                f"decision_at under {self.model}; a fill cannot happen at the "
                "instant the decision becomes possible"
            )


def earliest_execution(
    calendar: MarketCalendar,
    decision_at: datetime,
    *,
    model: ExecutionModel = ExecutionModel.NEXT_OPEN,
    bar_minutes: int | None = None,
) -> datetime:
    """The first instant a decision made at `decision_at` could fill.

    Holidays and long weekends fall out of this rather than being special
    cases: the calendar is asked for the next session, so a Friday-close
    decision before a Monday holiday lands on Tuesday's open without anything
    here knowing that a holiday exists.
    """
    moment = ensure_utc(decision_at, field="decision_at")

    if model is ExecutionModel.NEXT_OPEN:
        return calendar.next_tradable_open(moment)

    if bar_minutes is None:
        raise ValueError("NEXT_BAR needs bar_minutes to know how long a bar is")

    # The next bar opens the moment this one closes, so a decision taken at a
    # bar boundary fills at that same instant. Adding a bar length here would
    # skip a whole bar: `decision_at` is already the moment the data became
    # readable, which for a bar series *is* its close, which *is* the next
    # bar's open.
    #
    # Sharing a wall-clock timestamp is not the same-close leak. The ordering
    # BAR_CLOSE -> DECISION -> NEXT_BAR_OPEN is real, and the price taken is
    # the next bar's open, never the close that produced the decision — which
    # `PitReader.opening_price_at` is what actually enforces.
    if not calendar.is_open_at(moment):
        return calendar.next_tradable_open(moment)

    boundary = _bar_boundary_at_or_after(calendar, moment, bar_minutes)
    if calendar.is_open_at(boundary):
        return boundary
    return calendar.next_tradable_open(boundary)


def _bar_boundary_at_or_after(
    calendar: MarketCalendar, moment: datetime, bar_minutes: int
) -> datetime:
    """The first bar boundary at or after `moment`, counted from the open.

    A decision landing exactly on a boundary stays there. One landing inside a
    bar — a scheduler that fired a few seconds late, say — moves up to the next
    boundary rather than inventing a fill in the middle of a bar that has no
    price of its own.
    """
    session_start = calendar.session_open(moment.date())
    elapsed = (moment - session_start).total_seconds() / 60.0
    steps = math.ceil(elapsed / bar_minutes)
    return session_start + timedelta(minutes=steps * bar_minutes)


def decide(
    calendar: MarketCalendar,
    *,
    data_asof: datetime,
    decision_at: datetime,
    model: ExecutionModel = ExecutionModel.NEXT_OPEN,
    bar_minutes: int | None = None,
) -> ExecutionDecision:
    """Build a decision whose fill time is derived, not supplied.

    Deriving it is the point. A caller that passes its own `execution_at` can
    pass a wrong one; a caller that receives it cannot.
    """
    return ExecutionDecision(
        data_asof=ensure_utc(data_asof, field="data_asof"),
        decision_at=ensure_utc(decision_at, field="decision_at"),
        execution_at=earliest_execution(
            calendar, decision_at, model=model, bar_minutes=bar_minutes
        ),
        model=model,
    )


def assert_executable(
    calendar: MarketCalendar,
    *,
    decision_at: datetime,
    execution_at: datetime,
    model: ExecutionModel = ExecutionModel.NEXT_OPEN,
    bar_minutes: int | None = None,
) -> None:
    """Raise unless `execution_at` is no earlier than the rules allow.

    The engine calls this on every fill it records, including fills it derived
    itself. That is not redundant: the check is cheap, and it is the only thing
    standing between a future refactor and an equity curve that quietly
    improves.
    """
    earliest = earliest_execution(calendar, decision_at, model=model, bar_minutes=bar_minutes)
    actual = ensure_utc(execution_at, field="execution_at")
    if actual < earliest:
        raise ExecutionTimingError(
            f"fill at {actual.isoformat()} precedes the earliest tradable instant "
            f"{earliest.isoformat()} for a decision made at "
            f"{ensure_utc(decision_at, field='decision_at').isoformat()} "
            f"under {model}"
        )
