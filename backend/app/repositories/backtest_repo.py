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
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.backtest.strategies import StrategyDefinition
from app.core.types import SampleType
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
    """When and where a run happened, as opposed to which experiment it was.

    `data_snapshot_at` is not here. It is part of the experiment's identity,
    so it travels with the fields a later holdout is checked against rather
    than with the code version.
    """

    code: CodeVersion
    started_at: datetime


# Resolved once per process. See `resolve_commit` for why that is not merely
# an optimisation.
_RESOLVED: dict[Path, CodeVersion] = {}


def resolve_commit(repo_root: Path | None = None) -> CodeVersion:
    """The commit the code being run comes from, as of process start.

    **Cached for the life of the process, deliberately.** The obvious reading
    is that a run should resolve its commit when it starts, since resolving at
    persist time would record whatever HEAD happened to be once a long run
    finished. That is true as far as it goes, but it stops one step short:
    Python imported these modules before any run began, so editing or checking
    out files afterwards does not change the code that is executing. A worker
    that has been up for an hour is still running what it loaded.

    So the honest value is the one at import, and caching it is what makes the
    column mean "the code that ran" rather than "the code on disk when
    somebody asked". It is also 72ms cheaper per call, which is the lesser
    reason.

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
    if root in _RESOLVED:
        return _RESOLVED[root]

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

    version = CodeVersion(sha=sha, dirty=bool(dirty))
    _RESOLVED[root] = version
    return version


def save_run(
    session: Session,
    *,
    provenance: RunProvenance,
    **fields: object,
) -> BacktestRun:
    """Insert the run header.

    `fields` is whatever defines the experiment, passed through from the one
    place in the service that names those columns, so storing a run and
    checking a later holdout against it cannot describe different sets of
    them. Nothing here defaults: a row recording "the default cost model"
    becomes a different claim the day the default moves, and every stored run
    silently reinterprets itself.
    """
    run = BacktestRun(
        git_commit_sha=provenance.code.sha,
        git_dirty=provenance.code.dirty,
        started_at=provenance.started_at,
        **fields,
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
