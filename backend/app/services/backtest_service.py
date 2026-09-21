"""Orchestrating a backtest: resolve the snapshot, check the ground, run.

The engine is pure and cannot ask the database what data exists, which is
correct — it is what keeps the point-in-time filters unbypassable. But somebody
has to ask, because a run whose period reaches outside the data does not fail.
It simulates nothing for part of the span and then reports the result as a
full-period return.

Live, before this existed: a request for 2020-01-02 to 2026-09-18 against two
years of history returned `sessions = 1651` with an equity curve of 489 points
beginning in 2024. Every number in it was arithmetically correct and described
a different period from the one asked for.

This refuses instead. Silently trimming the window to what happens to be
stored would be the more convenient behaviour and the less honest one: the
caller asked a question about 2020, and the answer is that we cannot answer it,
not a differently-scoped answer that looks like the one requested.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.backtest import engine as bt
from app.backtest import walkforward as wf
from app.backtest.engine import BacktestResult, CostModel, Strategy
from app.backtest.execution import ExecutionModel
from app.backtest.metrics import Performance, summarise
from app.backtest.pit_repository import PitReader, coverage, snapshot_now
from app.backtest.walkforward import SampleType
from app.core.calendar import MarketCalendar
from app.core.types import Interval
from app.models import Instrument


class BacktestWindowError(Exception):
    """The requested period is not one the stored data can answer."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    """What to simulate. Every field is part of the run's identity."""

    instrument_id: int
    start: date
    end: date
    interval: Interval = Interval.DAY_1
    starting_cash: Decimal = Decimal("10000000")
    costs: CostModel | None = None
    execution_model: ExecutionModel = ExecutionModel.NEXT_OPEN
    bar_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """A finished run, with the coordinates needed to reproduce it."""

    result: BacktestResult
    data_snapshot_at: datetime
    coverage_start: date
    coverage_end: date


def execute(
    session: Session,
    strategy: Strategy,
    request: RunRequest,
    *,
    data_snapshot_at: datetime | None = None,
    require_complete_sessions: bool = True,
) -> RunOutcome:
    """Run `strategy` over `request`, refusing periods the data cannot cover.

    The snapshot is taken once, here, and used for both the coverage check and
    the simulation. Taking it twice would let a collection landing in between
    widen the data under a run whose bounds were already decided.

    Args:
        require_complete_sessions: refuse when a session inside the window
            produced no bar of its own. Default true. Such a session is marked
            at the last price that printed, which is right for a halt and
            wrong for a stretch the collector missed, and only the caller
            knows which they are looking at. Passing false proceeds anyway —
            deliberately, and the count stays on the result either way, so the
            decision is visible rather than absorbed.
    """
    instrument = session.get(Instrument, request.instrument_id)
    if instrument is None:
        raise BacktestWindowError(f"no instrument {request.instrument_id}")

    if request.end < request.start:
        raise BacktestWindowError(f"end {request.end} precedes start {request.start}")

    snapshot = data_snapshot_at if data_snapshot_at is not None else snapshot_now(session)

    span = coverage(session, request.instrument_id, request.interval, data_snapshot_at=snapshot)
    if span is None:
        raise BacktestWindowError(
            f"no {request.interval} data for {instrument.name} under the snapshot "
            f"{snapshot.isoformat()}; there is nothing to simulate"
        )

    calendar = MarketCalendar(instrument.market)

    # Compare sessions, not the dates that were typed. A window is asked for
    # in ordinary dates — "2025", "Q1", "through the end of November" — and
    # those boundaries land on weekends and holidays constantly. The run only
    # ever touches the sessions inside them, so a Sunday `end` one day past
    # the final session asks for nothing that is missing, and refusing it
    # would be refusing a question we can answer. Walk-forward makes this the
    # normal case rather than the odd one: every window boundary is a month,
    # quarter or year end.
    requested = calendar.sessions_between(request.start, request.end)
    if not requested:
        raise BacktestWindowError(
            f"no {instrument.market} trading sessions between {request.start} "
            f"and {request.end}; there is nothing to simulate"
        )

    first, last = span
    if requested[0] < first or requested[-1] > last:
        raise BacktestWindowError(
            f"requested {request.start}..{request.end}, which covers sessions "
            f"{requested[0]}..{requested[-1]}, but {instrument.name} has "
            f"{request.interval} data only for {first}..{last}. Trimming the window "
            "silently would report a shorter simulation as a full-period result"
        )
    result = bt.run(
        strategy,
        PitReader(session, data_snapshot_at=snapshot),
        instrument_id=request.instrument_id,
        calendar=calendar,
        start=request.start,
        end=request.end,
        interval=request.interval,
        starting_cash=request.starting_cash,
        costs=request.costs,
        execution_model=request.execution_model,
        bar_minutes=request.bar_minutes,
    )

    if require_complete_sessions and not result.simulated_full_period:
        # Coverage is judged on the outer dates, so a gap inside them — a
        # suspension, a collection that missed a stretch, or a session our
        # calendar believes in and the exchange did not — gets past the check
        # above. It still means the reported period is not the simulated one.
        missing = result.sessions_without_data
        shown = ", ".join(str(d) for d in missing[:5])
        raise BacktestWindowError(
            f"{len(missing)} of {result.sessions} sessions in {request.start}.."
            f"{request.end} produced no bar for {instrument.name} ({shown}"
            f"{', ...' if len(missing) > 5 else ''}); they were marked at the "
            "last price that printed, so the run would report a period it did "
            "not fully simulate. Pass require_complete_sessions=False to accept this"
        )

    return RunOutcome(
        result=result,
        data_snapshot_at=snapshot,
        coverage_start=first,
        coverage_end=last,
    )


@dataclass(frozen=True, slots=True)
class WindowResult:
    """One window's two measurements, and what stood behind them."""

    index: int
    sample_type: SampleType
    start: date
    end: date
    performance: Performance | None
    sessions: int
    trades: int
    abstained: int
    without_data: int
    unfilled: int


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """Every window, in and out of sample, with the caveats attached.

    `fitted` is the one to read first. When no fitting step was supplied, the
    same strategy ran on both sides of every split, and the in-sample figures
    are simply "how it did over those months" — not the period a rule was
    tuned on. An IN/OUT gap then says nothing about overfitting, and the UI
    must not present it as if it did. The flag is here so that claim cannot be
    made by accident.
    """

    windows: tuple[WindowResult, ...]
    fitted: bool
    data_snapshot_at: datetime
    holdout_start: date | None = None
    holdout_end: date | None = None

    def of(self, sample: SampleType) -> tuple[WindowResult, ...]:
        return tuple(w for w in self.windows if w.sample_type is sample)

    @property
    def evaluation_span(self) -> tuple[date, date] | None:
        """How much history the out-of-sample figures speak for."""
        out = self.of(SampleType.OUT_OF_SAMPLE)
        if not out:
            return None
        return out[0].start, out[-1].end


StrategyFitter = Callable[[Session, int, date, date], Strategy]
"""Chooses a strategy from a training period. Receives only that period."""


def walk_forward(
    session: Session,
    strategy: Strategy,
    request: RunRequest,
    *,
    train_sessions: int,
    eval_sessions: int,
    step_sessions: int | None = None,
    anchored: bool = False,
    holdout_sessions: int = 0,
    fit: StrategyFitter | None = None,
    data_snapshot_at: datetime | None = None,
    require_complete_sessions: bool = True,
) -> WalkForwardReport:
    """Run rolling train/evaluate splits across the requested period.

    One snapshot covers every window. Taking a fresh one per window would let
    a collection landing mid-run widen the data under the later windows only,
    which is the sort of difference that reads as the strategy improving.

    Args:
        strategy: used for both sides when `fit` is None.
        fit: given the training period, returns the strategy to evaluate with.
            It is handed the training dates and nothing else — the evaluation
            period is not passed, so a fitter cannot see what it will be
            judged on even if it wanted to.
    """
    instrument = session.get(Instrument, request.instrument_id)
    if instrument is None:
        raise BacktestWindowError(f"no instrument {request.instrument_id}")

    snapshot = data_snapshot_at if data_snapshot_at is not None else snapshot_now(session)
    calendar = MarketCalendar(instrument.market)
    sessions = calendar.sessions_between(request.start, request.end)

    split = wf.generate(
        sessions,
        train_sessions=train_sessions,
        eval_sessions=eval_sessions,
        step_sessions=step_sessions,
        anchored=anchored,
        holdout_sessions=holdout_sessions,
    )

    results: list[WindowResult] = []
    for window in split.windows:
        chosen = (
            fit(session, request.instrument_id, window.train_start, window.train_end)
            if fit is not None
            else strategy
        )
        for sample, lo, hi in (
            (SampleType.IN_SAMPLE, window.train_start, window.train_end),
            (SampleType.OUT_OF_SAMPLE, window.eval_start, window.eval_end),
        ):
            outcome = execute(
                session,
                chosen,
                replace(request, start=lo, end=hi),
                data_snapshot_at=snapshot,
                require_complete_sessions=require_complete_sessions,
            )
            result = outcome.result
            results.append(
                WindowResult(
                    index=window.index,
                    sample_type=sample,
                    start=lo,
                    end=hi,
                    performance=summarise(result.equity_curve, result.trades),
                    sessions=result.sessions,
                    trades=len(result.trades),
                    abstained=len(result.abstained_sessions),
                    without_data=len(result.sessions_without_data),
                    unfilled=len(result.unfilled),
                )
            )

    return WalkForwardReport(
        windows=tuple(results),
        fitted=fit is not None,
        data_snapshot_at=snapshot,
        holdout_start=split.holdout_start,
        holdout_end=split.holdout_end,
    )
