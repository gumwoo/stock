"""Backtest endpoints — the stored runs and what they recorded.

Read-only. Starting a run from the browser is deliberately absent: a
walk-forward takes minutes, and a button that quietly kicks one off is also a
button somebody holds down until the holdout says something they like. Runs
are started from the CLI, where the parameters are typed out and land in shell
history.

Every field the reproduction needs is exposed, because the screen's job is to
show that a result can be checked rather than to summarise it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import BacktestRunDetail, BacktestRunSummary, BacktestWindowOut
from app.db import get_db
from app.models import Instrument
from app.models.backtest import BacktestRun
from app.repositories import backtest_repo, instrument_repo

router = APIRouter(prefix="/api/backtests", tags=["backtests"])

SessionDep = Annotated[Session, Depends(get_db)]


def _summary(session: Session, run: BacktestRun) -> BacktestRunSummary:
    instrument = session.get(Instrument, run.instrument_id)
    windows = backtest_repo.windows_of(session, run.id)
    holdout = next((w for w in windows if w.sample_type == "HOLDOUT"), None)

    return BacktestRunSummary(
        id=run.id,
        instrument_id=run.instrument_id,
        symbol=instrument_repo.current_symbol(session, run.instrument_id) or "?",
        name=instrument.name if instrument else f"instrument {run.instrument_id}",
        strategy_kind=run.strategy_kind,
        strategy_version=run.strategy_version,
        strategy_params=dict(run.strategy_params),
        fitter_version=run.fitter_version,
        period_start=run.period_start,
        period_end=run.period_end,
        started_at=run.started_at,
        windows=len([w for w in windows if w.sample_type != "HOLDOUT"]),
        has_holdout=holdout is not None,
    )


def _window(row: object) -> BacktestWindowOut:
    return BacktestWindowOut(
        window_index=row.window_index,  # type: ignore[attr-defined]
        sample_type=str(row.sample_type),  # type: ignore[attr-defined]
        period_start=row.period_start,  # type: ignore[attr-defined]
        period_end=row.period_end,  # type: ignore[attr-defined]
        strategy=f"{row.chosen_kind}@{row.chosen_version}",  # type: ignore[attr-defined]
        strategy_params=dict(row.chosen_params),  # type: ignore[attr-defined]
        sessions=row.sessions,  # type: ignore[attr-defined]
        observations=row.observations,  # type: ignore[attr-defined]
        total_return=_f(row.total_return),  # type: ignore[attr-defined]
        cagr=_f(row.cagr),  # type: ignore[attr-defined]
        max_drawdown=_f(row.max_drawdown),  # type: ignore[attr-defined]
        sharpe=_f(row.sharpe),  # type: ignore[attr-defined]
        win_rate=_f(row.win_rate),  # type: ignore[attr-defined]
        profit_factor=_f(row.profit_factor),  # type: ignore[attr-defined]
        trades=row.trades,  # type: ignore[attr-defined]
        abstained=row.abstained,  # type: ignore[attr-defined]
        without_data=row.without_data,  # type: ignore[attr-defined]
        unfilled=row.unfilled,  # type: ignore[attr-defined]
    )


def _f(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


@router.get("", response_model=list[BacktestRunSummary])
def list_runs(
    session: SessionDep,
    instrument_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[BacktestRunSummary]:
    stmt = select(BacktestRun).order_by(BacktestRun.started_at.desc()).limit(limit)
    if instrument_id is not None:
        stmt = stmt.where(BacktestRun.instrument_id == instrument_id)
    return [_summary(session, run) for run in session.execute(stmt).scalars()]


@router.get("/{run_id}", response_model=BacktestRunDetail)
def get_run(run_id: int, session: SessionDep) -> BacktestRunDetail:
    run = backtest_repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no backtest run {run_id}")

    instrument = session.get(Instrument, run.instrument_id)
    windows = backtest_repo.windows_of(session, run.id)

    return BacktestRunDetail(
        **_summary(session, run).model_dump(),
        market=instrument.market.value if instrument else "?",
        interval=str(run.interval),
        strategy_fingerprint=run.strategy_fingerprint,
        fit_trace_fingerprint=run.fit_trace_fingerprint,
        holdout_strategy_fingerprint=run.holdout_strategy_fingerprint,
        git_commit_sha=run.git_commit_sha,
        git_dirty=run.git_dirty,
        data_snapshot_at=run.data_snapshot_at,
        starting_cash=float(run.starting_cash),
        commission_bps=float(run.commission_bps),
        slippage_bps=float(run.slippage_bps),
        min_commission=float(run.min_commission),
        execution_model=run.execution_model,
        bar_minutes=run.bar_minutes,
        universe=run.universe,
        train_sessions=run.train_sessions,
        eval_sessions=run.eval_sessions,
        anchored=run.anchored,
        require_complete_sessions=run.require_complete_sessions,
        holdout_start=run.holdout_start,
        holdout_end=run.holdout_end,
        window_rows=[_window(w) for w in windows],
    )
