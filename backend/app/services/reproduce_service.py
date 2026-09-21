"""Re-executing a stored run and checking it comes out the same.

Everything before this made a run *describable*. This is what makes the
description worth anything: a stored row is rebuilt into a request, run again
under its own data snapshot, and compared against what was recorded.

**A reproduction is a comparison, not a claim.** It returns what matched and
what did not, and a mismatch is a finding rather than an error — that is the
whole point of running it. What *is* an error is being unable to attempt the
comparison: a strategy whose kind this build no longer knows, or a run whose
rows have gone.

**The commit is checked but does not block.** A run reproduced from different
code is still worth reproducing; if the numbers match anyway, that is
information, and if they do not, the commit is the first thing to look at. So
the mismatch is reported rather than raised, and `git_dirty` is reported too,
since a run made from uncommitted edits cannot be reproduced from its commit
at all.

**A fitted run cannot be reproduced from its header.** The header holds a
placeholder; the fitter that chose the parameters is code, not data, and the
row cannot rebuild it. What *can* be checked is that each window's recorded
choice still produces the recorded result, so a fitted run is replayed
window-by-window from the definitions its own rows hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.backtest.engine import CostModel
from app.backtest.execution import ExecutionModel
from app.backtest.metrics import Performance, summarise
from app.backtest.strategies import StrategyDefinition, UnknownStrategyError
from app.core.types import SampleType
from app.models.backtest import BacktestRun, BacktestWindow
from app.repositories import backtest_repo
from app.services import backtest_service as svc


class ReproduceError(Exception):
    """The comparison could not be attempted."""


@dataclass(frozen=True, slots=True)
class WindowComparison:
    """One stored measurement against its re-execution."""

    window_index: int
    sample_type: SampleType
    start: date
    end: date
    stored_return: float | None
    replayed_return: float | None
    stored_trades: int
    replayed_trades: int

    @property
    def matches(self) -> bool:
        return (
            _same(self.stored_return, self.replayed_return)
            and self.stored_trades == self.replayed_trades
        )

    def describe(self) -> str:
        if self.matches:
            return f"{self.sample_type} #{self.window_index} {self.start}..{self.end} matches"
        return (
            f"{self.sample_type} #{self.window_index} {self.start}..{self.end}: "
            f"return {_show(self.stored_return)} -> {_show(self.replayed_return)}, "
            f"trades {self.stored_trades} -> {self.replayed_trades}"
        )


@dataclass(frozen=True, slots=True)
class Reproduction:
    """What a stored run produces when it is run again."""

    run_id: int
    windows: tuple[WindowComparison, ...]
    code_matches: bool
    stored_commit: str
    current_commit: str
    stored_was_dirty: bool

    @property
    def reproduced(self) -> bool:
        """Whether every stored measurement came back identical."""
        return bool(self.windows) and all(w.matches for w in self.windows)

    @property
    def mismatches(self) -> tuple[WindowComparison, ...]:
        return tuple(w for w in self.windows if not w.matches)

    def summary(self) -> str:
        head = (
            f"run #{self.run_id}: {len(self.windows) - len(self.mismatches)}"
            f"/{len(self.windows)} windows reproduced"
        )
        notes = []
        if not self.code_matches:
            notes.append(f"code differs ({self.stored_commit[:8]} -> {self.current_commit[:8]})")
        if self.stored_was_dirty:
            notes.append("stored run had uncommitted changes, so its commit is not the whole code")
        return head + (f" — {'; '.join(notes)}" if notes else "")


def reproduce(session: Session, run_id: int) -> Reproduction:
    """Re-run a stored backtest from its own row and compare.

    Every window is replayed from the definition that window recorded, over
    that window's own period, under the run's data snapshot. A fixed run
    therefore replays the same strategy throughout and a fitted one replays
    each fold's choice — which is as far as a stored row can go, since the
    fitter itself is code.
    """
    run = backtest_repo.get_run(session, run_id)
    if run is None:
        raise ReproduceError(f"no backtest run {run_id}")

    stored = [
        w
        for w in backtest_repo.windows_of(session, run_id)
        if w.sample_type is not SampleType.HOLDOUT
    ]
    if not stored:
        raise ReproduceError(
            f"run {run_id} has no window rows; there is nothing to compare against"
        )

    comparisons = tuple(_replay(session, run, window) for window in stored)
    current = backtest_repo.resolve_commit()

    return Reproduction(
        run_id=run_id,
        windows=comparisons,
        code_matches=current.sha == run.git_commit_sha,
        stored_commit=run.git_commit_sha,
        current_commit=current.sha,
        stored_was_dirty=run.git_dirty,
    )


def _replay(session: Session, run: BacktestRun, window: BacktestWindow) -> WindowComparison:
    definition = StrategyDefinition(
        kind=window.chosen_kind,
        version=window.chosen_version,
        params=window.chosen_params,
    )
    try:
        strategy = definition.build()
    except UnknownStrategyError as exc:
        raise ReproduceError(
            f"run {run.id} window {window.window_index} ran {definition.describe()}, "
            f"which this build cannot construct: {exc}"
        ) from exc

    outcome = svc.execute(
        session,
        strategy,
        svc.RunRequest(
            instrument_id=run.instrument_id,
            start=window.period_start,
            end=window.period_end,
            interval=run.interval,
            starting_cash=run.starting_cash,
            costs=CostModel(
                commission_bps=run.commission_bps,
                slippage_bps=run.slippage_bps,
                min_commission=run.min_commission,
            ),
            execution_model=ExecutionModel(run.execution_model),
            bar_minutes=run.bar_minutes,
        ),
        data_snapshot_at=run.data_snapshot_at,
        require_complete_sessions=run.require_complete_sessions,
    )
    performance: Performance | None = summarise(outcome.result.equity_curve, outcome.result.trades)

    return WindowComparison(
        window_index=window.window_index,
        sample_type=window.sample_type,
        start=window.period_start,
        end=window.period_end,
        stored_return=_as_float(window.total_return),
        replayed_return=performance.total_return if performance else None,
        stored_trades=window.trades,
        replayed_trades=len(outcome.result.trades),
    )


def _as_float(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def _same(stored: float | None, replayed: float | None) -> bool:
    """Equal to the precision the column stores.

    The engine works in Decimal and the column holds eight decimal places, so
    a replay that agrees exactly still differs in the digits that were never
    written down. Comparing at the stored precision compares what was stored.
    """
    if stored is None or replayed is None:
        return stored is replayed
    return abs(stored - replayed) < 1e-8


def _show(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.6f}"
