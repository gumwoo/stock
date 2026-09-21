"""Persisting a run and reading it back.

The tables carry provenance; this module is what puts it there honestly.

Two things it refuses to fudge. A run without a resolvable commit is not
stored, because the strategy and the data are only two of the three axes and a
row claiming reproducibility on two of them is worse than no row. And a
holdout can be written once per run — enforced by a unique constraint rather
than by a check-then-insert, so two concurrent attempts cannot both pass the
check.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.backtest.strategies import StrategyDefinition
from app.core.types import Interval, SampleType
from app.models.backtest import BacktestRun, BacktestWindow


class ProvenanceError(Exception):
    """A run cannot be stored because its coordinates are incomplete."""


class HoldoutAlreadyRecordedError(Exception):
    """This run already has its one holdout measurement."""


@dataclass(frozen=True, slots=True)
class CodeVersion:
    """Which code produced a run, and whether that is the whole story."""

    sha: str
    dirty: bool

    def __str__(self) -> str:
        return f"{self.sha}{' (dirty)' if self.dirty else ''}"


@dataclass(frozen=True, slots=True)
class RunProvenance:
    """The non-strategy coordinates of a run."""

    code: CodeVersion
    data_snapshot_at: datetime
    started_at: datetime


def resolve_commit(repo_root: Path | None = None) -> CodeVersion:
    """The commit the code being run comes from.

    Raises rather than returning a placeholder. "unknown" in this column would
    be indistinguishable from a real value at a glance and would quietly
    destroy the one axis the strategy definition and the data snapshot cannot
    cover: a change to the code behind the strategy kind.

    A dirty working tree is reported alongside the sha rather than smuggled
    into it. A run made from uncommitted edits is not reproducible from the
    commit alone, and the row must not imply otherwise — but the sha column
    holds a sha, so the flag is its own field.
    """
    root = repo_root or Path(__file__).resolve().parents[3]
    try:
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvenanceError(
            f"cannot resolve the current commit from {root}: {exc}. A stored run "
            "without one claims to be reproducible from a strategy and a data "
            "snapshot alone, which is two of the three things that decide a result"
        ) from exc

    if not sha:
        raise ProvenanceError(f"git reported no HEAD commit in {root}")
    return CodeVersion(sha=sha, dirty=bool(dirty))


def save_run(
    session: Session,
    *,
    instrument_id: int,
    definition: StrategyDefinition,
    fitter_version: str | None,
    provenance: RunProvenance,
    interval: Interval,
    period_start: object,
    period_end: object,
    starting_cash: Decimal,
    commission_bps: Decimal,
    slippage_bps: Decimal,
    min_commission: Decimal,
    execution_model: str,
    bar_minutes: int | None,
    train_sessions: int,
    eval_sessions: int,
    anchored: bool,
    holdout_start: object,
    holdout_end: object,
    require_complete_sessions: bool,
) -> BacktestRun:
    """Insert the run header. Costs are the values that were applied.

    Nothing here defaults: a column recording "the default cost model" becomes
    a different claim the day the default moves, and every stored run silently
    reinterprets itself.
    """
    run = BacktestRun(
        instrument_id=instrument_id,
        strategy_kind=definition.kind,
        strategy_version=definition.version,
        # MappingProxyType does not serialise; JSONB needs a plain dict.
        strategy_params=dict(definition.params),
        strategy_fingerprint=definition.fingerprint,
        fitter_version=fitter_version,
        git_commit_sha=provenance.code.sha,
        git_dirty=provenance.code.dirty,
        data_snapshot_at=provenance.data_snapshot_at,
        interval=interval,
        period_start=period_start,
        period_end=period_end,
        starting_cash=starting_cash,
        commission_bps=commission_bps,
        slippage_bps=slippage_bps,
        min_commission=min_commission,
        execution_model=execution_model,
        bar_minutes=bar_minutes,
        train_sessions=train_sessions,
        eval_sessions=eval_sessions,
        anchored=anchored,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        require_complete_sessions=require_complete_sessions,
        started_at=provenance.started_at,
    )
    session.add(run)
    session.flush()
    return run


def save_window(
    session: Session,
    run: BacktestRun,
    *,
    window_index: int,
    sample_type: SampleType,
    period_start: object,
    period_end: object,
    chosen: StrategyDefinition,
    sessions: int,
    observations: int,
    total_return: float | None,
    cagr: float | None,
    max_drawdown: float | None,
    sharpe: float | None,
    win_rate: float | None,
    profit_factor: float | None,
    trades: int,
    abstained: int,
    without_data: int,
    unfilled: int,
) -> BacktestWindow:
    """Insert one measurement.

    A holdout row collides with any existing one for the same run, which is
    what makes "evaluated once" a property of the data rather than a habit.
    """
    window = BacktestWindow(
        run_id=run.id,
        window_index=window_index,
        sample_type=sample_type,
        period_start=period_start,
        period_end=period_end,
        chosen_kind=chosen.kind,
        chosen_version=chosen.version,
        chosen_params=dict(chosen.params),
        chosen_fingerprint=chosen.fingerprint,
        sessions=sessions,
        observations=observations,
        total_return=total_return,
        cagr=cagr,
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        win_rate=win_rate,
        profit_factor=profit_factor,
        trades=trades,
        abstained=abstained,
        without_data=without_data,
        unfilled=unfilled,
    )
    session.add(window)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if sample_type is SampleType.HOLDOUT:
            raise HoldoutAlreadyRecordedError(
                f"run {run.id} already has a holdout measurement. It is evaluated "
                "once, after every choice has been made; a second one would be a "
                "second opinion on the only period nothing was allowed to iterate "
                "against"
            ) from exc
        raise
    return window


def get_run(session: Session, run_id: int) -> BacktestRun | None:
    return session.get(BacktestRun, run_id)


def windows_of(
    session: Session, run_id: int, *, sample_type: SampleType | None = None
) -> list[BacktestWindow]:
    stmt = select(BacktestWindow).where(BacktestWindow.run_id == run_id)
    if sample_type is not None:
        stmt = stmt.where(BacktestWindow.sample_type == sample_type)
    return list(session.execute(stmt.order_by(BacktestWindow.id)).scalars())


def holdout_of(session: Session, run_id: int) -> BacktestWindow | None:
    rows = windows_of(session, run_id, sample_type=SampleType.HOLDOUT)
    return rows[0] if rows else None


def runs_for(session: Session, instrument_id: int, *, limit: int = 50) -> list[BacktestRun]:
    stmt = (
        select(BacktestRun)
        .where(BacktestRun.instrument_id == instrument_id)
        .order_by(BacktestRun.started_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())
