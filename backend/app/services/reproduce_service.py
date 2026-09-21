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

import json
from collections.abc import Mapping, Sequence
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
    integrity: tuple[str, ...]
    code_matches: bool
    stored_commit: str
    current_commit: str
    stored_was_dirty: bool

    @property
    def reproduced(self) -> bool:
        """Whether this is the stored experiment, re-run, coming back the same.

        Both halves are needed. The measurements agreeing says the engine
        still produces what the rows describe; the integrity findings being
        empty says those rows are the ones that were written. Replaying a
        tampered row against its own figures satisfies the first on its own.
        """
        return not self.integrity and bool(self.windows) and all(w.matches for w in self.windows)

    @property
    def mismatches(self) -> tuple[WindowComparison, ...]:
        return tuple(w for w in self.windows if not w.matches)

    def summary(self) -> str:
        head = (
            f"run #{self.run_id}: {len(self.windows) - len(self.mismatches)} of {len(self.windows)} windows reproduced"
            if self.windows
            else f"run #{self.run_id}: not compared"
        )
        notes = []
        if self.integrity:
            notes.append(f"{len(self.integrity)} integrity findings")
        if not self.code_matches:
            notes.append(f"code differs ({self.stored_commit[:8]} -> {self.current_commit[:8]})")
        if self.stored_was_dirty:
            notes.append("stored run had uncommitted changes, so its commit is not the whole code")
        return head + (f" — {'; '.join(notes)}" if notes else "")


def check_integrity(run: BacktestRun, windows: Sequence[BacktestWindow]) -> tuple[str, ...]:
    """What the stored rows claim about each other, verified.

    Re-running a backtest proves the engine still produces what the rows say.
    It cannot prove the rows are the ones that were written — replaying from a
    tampered row and comparing against that row's own figures agrees with
    itself perfectly. Probed against the live database before this existed:

        run.strategy_params changed, fingerprint stale  -> reproduced=True
        run.fit_trace_fingerprint no longer matches     -> reproduced=True
        one window row deleted                          -> reproduced=True
        run.holdout_start/end changed after the fact    -> reproduced=True

    The third is the one that should worry anybody: drop the window that did
    worst and the run still reports as reproduced.

    Every check here is a claim one row makes about another, so none of them
    needs the market data or the engine — which is also why they run first.
    """
    findings: list[str] = []

    header = StrategyDefinition(
        kind=run.strategy_kind, version=run.strategy_version, params=run.strategy_params
    )
    if header.fingerprint != run.strategy_fingerprint:
        findings.append(
            f"the run header's strategy fingerprint is {run.strategy_fingerprint}, but "
            f"its kind, version and params digest to {header.fingerprint}"
        )

    for window in windows:
        chosen = StrategyDefinition(
            kind=window.chosen_kind,
            version=window.chosen_version,
            params=window.chosen_params,
        )
        if chosen.fingerprint != window.chosen_fingerprint:
            findings.append(
                f"window {window.window_index} ({window.sample_type}) records fingerprint "
                f"{window.chosen_fingerprint} for a strategy that digests to "
                f"{chosen.fingerprint}"
            )

    measured = [w for w in windows if w.sample_type is not SampleType.HOLDOUT]

    # What the header says it ran, against what the windows say they ran.
    # Both fingerprints can be recomputed and still disagree with each other:
    # rewrite the header's strategy, recompute its digest, and every check
    # above passes while the header describes a different experiment from the
    # one the rows record.
    ran = {
        (w.chosen_kind, w.chosen_version, json.dumps(w.chosen_params, sort_keys=True))
        for w in measured
    }
    if run.fitter_version is None:
        # A fixed run ran one strategy, and the header is that strategy.
        if len(ran) > 1:
            findings.append(
                f"the run records no fitter, so every window ran one strategy — but "
                f"its windows ran {len(ran)} different ones"
            )
        elif ran:
            kind, version, params = next(iter(ran))
            if (kind, version, params) != (
                run.strategy_kind,
                run.strategy_version,
                json.dumps(run.strategy_params, sort_keys=True),
            ):
                findings.append(
                    f"the run header ran {run.strategy_kind}@{run.strategy_version} "
                    f"{run.strategy_params}, but every window ran {kind}@{version} {params}"
                )
    else:
        # A fitted run has no single strategy, so its header is a placeholder
        # over the fitter. Inventing a `fitter_version` on a fixed run would
        # relabel it as one whose parameters were chosen — which is the flag
        # that decides whether an in/out gap means anything at all.
        expected_kinds = {k for k, _, _ in ran}
        expected_kind = next(iter(expected_kinds)) if len(expected_kinds) == 1 else "mixed"
        out_of_sample = sum(1 for w in measured if w.sample_type is SampleType.OUT_OF_SAMPLE)
        expected = {"fitted": True, "windows": out_of_sample}
        if run.strategy_version != run.fitter_version:
            findings.append(
                f"the run records fitter {run.fitter_version} but its header version is "
                f"{run.strategy_version}; a fitted run is headed by its fitter"
            )
        if run.strategy_kind != expected_kind:
            findings.append(
                f"the run header names kind {run.strategy_kind}, but its windows ran "
                f"{expected_kind}"
            )
        if run.strategy_params != expected:
            findings.append(
                f"the run header's placeholder is {run.strategy_params}, but its windows "
                f"describe {expected}"
            )
    trace = svc.fit_trace_fingerprint_of(
        (
            w.window_index,
            w.sample_type,
            w.period_start,
            w.period_end,
            StrategyDefinition(
                kind=w.chosen_kind, version=w.chosen_version, params=w.chosen_params
            ).canonical,
        )
        for w in measured
    )
    if trace != run.fit_trace_fingerprint:
        findings.append(
            f"the run header's fit trace is {run.fit_trace_fingerprint}, but its "
            f"{len(measured)} window rows digest to {trace} — they are not the windows "
            "this run recorded, whether one was altered, removed or added"
        )

    for window in windows:
        if window.period_start < run.period_start or window.period_end > run.period_end:
            findings.append(
                f"window {window.window_index} ({window.sample_type}) covers "
                f"{window.period_start}..{window.period_end}, outside the run's "
                f"{run.period_start}..{run.period_end}"
            )

    if (run.holdout_start is None) != (run.holdout_end is None):
        findings.append("the run reserves half a holdout: one of its two dates is missing")
    elif run.holdout_start is not None and run.holdout_end is not None:
        if run.holdout_start < run.period_start or run.holdout_end > run.period_end:
            findings.append(
                f"the reserved holdout {run.holdout_start}..{run.holdout_end} falls outside "
                f"the run's period {run.period_start}..{run.period_end}"
            )
        if measured and run.holdout_start <= max(w.period_end for w in measured):
            findings.append(
                f"the reserved holdout opens {run.holdout_start}, on or before a measured "
                f"window ends — a holdout no window may reach cannot overlap one"
            )

    holdouts = [w for w in windows if w.sample_type is SampleType.HOLDOUT]

    # Which strategy the final measurement used. A fitted run's last refit
    # need not match any fold's choice, so the fit trace does not cover it;
    # a fixed run's must be the header's strategy, and either way the run
    # records the fingerprint when the holdout is stored. Without it the
    # holdout row could be rewritten to a different strategy, its figures
    # recomputed to match, and the whole run still report as reproduced.
    if holdouts and run.holdout_strategy_fingerprint is None:
        findings.append(
            "the run has a holdout measurement but records no strategy for it, so "
            "nothing ties that measurement to this experiment"
        )
    for holdout in holdouts:
        if (
            run.holdout_strategy_fingerprint is not None
            and holdout.chosen_fingerprint != run.holdout_strategy_fingerprint
        ):
            findings.append(
                f"the holdout row ran {holdout.chosen_kind}@{holdout.chosen_version} "
                f"({holdout.chosen_fingerprint}), but the run records its holdout as "
                f"{run.holdout_strategy_fingerprint}"
            )
        if run.fitter_version is None and holdout.chosen_fingerprint != run.strategy_fingerprint:
            findings.append(
                f"the run records no fitter, so its holdout ran the header's strategy — "
                f"but the holdout row ran {holdout.chosen_kind}@{holdout.chosen_version}"
            )
    if not holdouts and run.holdout_strategy_fingerprint is not None:
        findings.append(
            f"the run records a holdout strategy ({run.holdout_strategy_fingerprint}) "
            "but has no holdout measurement"
        )
    if len(holdouts) > 1:
        findings.append(f"the run has {len(holdouts)} holdout rows; it may have one")
    for holdout in holdouts:
        if (holdout.period_start, holdout.period_end) != (run.holdout_start, run.holdout_end):
            findings.append(
                f"the holdout row covers {holdout.period_start}..{holdout.period_end}, but "
                f"the run reserved {run.holdout_start}..{run.holdout_end}"
            )

    return tuple(findings)


def reproduce(session: Session, run_id: int) -> Reproduction:
    """Re-run a stored backtest from its own row and compare.

    Every window is replayed from the definition that window recorded, over
    that window's own period, under the run's data snapshot. A fixed run
    therefore replays the same strategy throughout and a fitted one replays
    each fold's choice — which is as far as a stored row can go, since the
    fitter itself is code.

    **The holdout is replayed too.** Evaluating it is a once-only act; this is
    not that. Nothing is chosen here and no fitter runs — the row's own
    strategy is re-executed over the row's own period to check the figures
    were recorded correctly. Leaving it out meant the final verdict of an
    experiment was the one number nobody checked:

        UPDATE backtest_window SET total_return = 99, sharpe = 999,
               trades = 999 WHERE sample_type = 'HOLDOUT';

        reproduced = True
    """
    run = backtest_repo.get_run(session, run_id)
    if run is None:
        raise ReproduceError(f"no backtest run {run_id}")

    stored = backtest_repo.windows_of(session, run_id)
    if not stored:
        raise ReproduceError(
            f"run {run_id} has no window rows; there is nothing to compare against"
        )

    current = backtest_repo.resolve_commit()
    integrity = check_integrity(run, backtest_repo.windows_of(session, run_id))
    if integrity:
        # The rows are not the ones that were written, so replaying them
        # answers a question nobody asked — and a tampered period would raise
        # out of the replay before these findings could be reported at all.
        return Reproduction(
            run_id=run_id,
            windows=(),
            integrity=integrity,
            code_matches=current.sha == run.git_commit_sha,
            stored_commit=run.git_commit_sha,
            current_commit=current.sha,
            stored_was_dirty=run.git_dirty,
        )

    comparisons = tuple(_replay(session, run, window) for window in stored)

    return Reproduction(
        run_id=run_id,
        windows=comparisons,
        integrity=integrity,
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
