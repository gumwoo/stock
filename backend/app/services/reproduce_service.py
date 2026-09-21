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

from collections.abc import Mapping
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


# Every figure a window row records. Compared as a list rather than one by
# one, so a measurement added to the table is checked from the moment it is
# stored — the same reason `experiment_fields` exists on the run header.
MEASUREMENTS: tuple[str, ...] = (
    "sessions",
    "observations",
    "total_return",
    "cagr",
    "max_drawdown",
    "sharpe",
    "win_rate",
    "profit_factor",
    "trades",
    "abstained",
    "without_data",
    "unfilled",
)


@dataclass(frozen=True, slots=True)
class WindowComparison:
    """One stored measurement against its re-execution, in full.

    An earlier version compared the total return and the trade count, and
    called the result "reproduced". Everything else in the row could be
    anything at all:

        UPDATE backtest_window SET sharpe = 999, max_drawdown = 0.99,
               abstained = 999, unfilled = 999, without_data = 999,
               observations = 1, sessions = 1, cagr = 42, win_rate = 1,
               profit_factor = 77;

        reproduced = True   mismatches = 0

    The caveat columns matter most there. A run whose abstentions or
    stale-marked sessions were wrong is a run whose numbers describe a
    different experiment, and those are exactly the fields a summary would
    never show.
    """

    window_index: int
    sample_type: SampleType
    start: date
    end: date
    stored: Mapping[str, float | int | None]
    replayed: Mapping[str, float | int | None]

    @property
    def differences(self) -> tuple[str, ...]:
        return tuple(
            name for name in MEASUREMENTS if not _same(self.stored[name], self.replayed[name])
        )

    @property
    def matches(self) -> bool:
        return not self.differences

    def describe(self) -> str:
        where = f"{self.sample_type} #{self.window_index} {self.start}..{self.end}"
        if self.matches:
            return f"{where} matches"
        changes = ", ".join(
            f"{name} {_show(self.stored[name])} -> {_show(self.replayed[name])}"
            for name in self.differences
        )
        return f"{where}: {changes}"


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
        """Whether every stored measurement of every window came back identical."""
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
    result = outcome.result

    replayed: dict[str, float | int | None] = {
        "sessions": result.sessions,
        "observations": performance.observations if performance else 0,
        "total_return": performance.total_return if performance else None,
        "cagr": performance.cagr if performance else None,
        "max_drawdown": performance.max_drawdown if performance else None,
        "sharpe": performance.sharpe if performance else None,
        "win_rate": performance.win_rate if performance else None,
        "profit_factor": performance.profit_factor if performance else None,
        "trades": len(result.trades),
        "abstained": len(result.abstained_sessions),
        "without_data": len(result.sessions_without_data),
        "unfilled": len(result.unfilled),
    }

    return WindowComparison(
        window_index=window.window_index,
        sample_type=window.sample_type,
        start=window.period_start,
        end=window.period_end,
        stored={name: _as_float(getattr(window, name)) for name in MEASUREMENTS},
        replayed={name: _as_float(value) for name, value in replayed.items()},
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
