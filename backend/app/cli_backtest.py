"""Backtest commands: run one, read one back, re-run one and compare.

Three verbs, and the distinction between them is the point of the whole
persistence layer:

    backtest run        walk forward and store the result
    backtest show       what a stored run recorded
    backtest reproduce  run it again from its rows and say what differs

`run` deliberately does not take the holdout. That is `backtest holdout`, a
separate command a person has to mean — a holdout reported on every run gets
fitted by eye, which is harder to notice than fitting it in code and no less
real. `--with-holdout` exists for the case where the choices are already made
and both belong in one breath, and for fitted runs, whose fitter is code and
cannot be rebuilt from a stored row later.

Output is plain columns rather than a table library. These numbers get pasted
into issues and commit messages, and something that survives copy-paste is
worth more here than something that looks better in a terminal.
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from sqlalchemy.orm import Session

from app.backtest.engine import CostModel
from app.backtest.execution import ExecutionModel
from app.backtest.pit_repository import coverage, snapshot_now
from app.backtest.strategies import StrategyDefinition, buy_and_hold, moving_average_cross
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.core.types import Interval, SampleType
from app.db import session_scope
from app.models import Instrument
from app.models.backtest import BacktestRun
from app.repositories import backtest_repo, instrument_repo
from app.services import backtest_service as svc
from app.services import reproduce_service as rs
from app.services.backtest_service import RunRequest, StrategySpec

STRATEGIES = {
    "ma": lambda short, long: moving_average_cross(short=short, long=long),
    "hold": lambda short, long: buy_and_hold(),
}


def _resolve(session: Session, symbol: str) -> Instrument:
    today = utc_now().date()
    for market in Market:
        found = instrument_repo.resolve_symbol(session, symbol, market, asof=today)
        if found is not None:
            return found
    raise SystemExit(f"no instrument for symbol {symbol!r}; try `python -m app.cli seed`")


def _pct(value: float | None) -> str:
    return "      n/a" if value is None else f"{value:+8.2%}"


def _num(value: float | None, places: int = 2) -> str:
    return "    n/a" if value is None else f"{value:>7.{places}f}"


def cmd_run(
    symbol: str,
    strategy: str,
    short: int,
    long: int,
    train: int,
    evaluate: int,
    holdout: int,
    anchored: bool,
    commission_bps: str,
    slippage_bps: str,
    cash: str,
    allow_missing: bool,
    with_holdout: bool,
) -> int:
    """Walk forward over everything stored for this instrument, and persist it."""
    definition: StrategyDefinition = STRATEGIES[strategy](short, long)

    with session_scope() as session:
        instrument = _resolve(session, symbol)
        snapshot = snapshot_now(session)
        span = coverage(
            session, instrument.instrument_id, Interval.DAY_1, data_snapshot_at=snapshot
        )
        if span is None:
            raise SystemExit(f"no daily bars stored for {symbol}")

        report = svc.walk_forward(
            session,
            StrategySpec(definition=definition),
            RunRequest(
                instrument_id=instrument.instrument_id,
                start=span[0],
                end=span[1],
                starting_cash=Decimal(cash),
                costs=CostModel(Decimal(commission_bps), Decimal(slippage_bps)),
                execution_model=ExecutionModel.NEXT_OPEN,
            ),
            train_sessions=train,
            eval_sessions=evaluate,
            holdout_sessions=holdout,
            anchored=anchored,
            data_snapshot_at=snapshot,
            require_complete_sessions=not allow_missing,
        )
        run = svc.persist(session, report)
        session.commit()

        print(f"run #{run.id}  {instrument.name} ({symbol})  {definition.describe()}")
        _print_windows(session, run.id)
        print()
        if with_holdout:
            stored = svc.evaluate_and_persist_holdout(session, run, report)
            session.commit()
            print(
                f"  HOLDOUT {stored.period_start}..{stored.period_end}"
                f"   return {_pct(_f(stored.total_return))}   trades {stored.trades}"
            )
        else:
            print(f"  holdout reserved {run.holdout_start}..{run.holdout_end}, not taken")
            print("  take it once, when the choices are made:")
            print(f"      python -m app.cli backtest holdout --run {run.id}")
    return 0


def cmd_show(run_id: int) -> int:
    """Everything a stored run recorded, including what would reproduce it."""
    with session_scope() as session:
        run = backtest_repo.get_run(session, run_id)
        if run is None:
            raise SystemExit(f"no backtest run {run_id}")
        instrument = session.get(Instrument, run.instrument_id)
        name = instrument.name if instrument else f"instrument {run.instrument_id}"

        print(f"run #{run.id}  {name}")
        print(f"  strategy      {run.strategy_kind}@{run.strategy_version} {run.strategy_params}")
        print(f"  fingerprint   {run.strategy_fingerprint}   fit trace {run.fit_trace_fingerprint}")
        if run.fitter_version:
            print(f"  fitter        {run.fitter_version}")
        dirty = "  (uncommitted changes)" if run.git_dirty else ""
        print(f"  code          {run.git_commit_sha}{dirty}")
        print(f"  data snapshot {run.data_snapshot_at.isoformat()}")
        print(f"  period        {run.period_start}..{run.period_end}  {run.interval}")
        print(
            f"  costs         commission {run.commission_bps}bp  slippage "
            f"{run.slippage_bps}bp  min {run.min_commission}"
        )
        print(f"  execution     {run.execution_model}  cash {run.starting_cash}")
        print(
            f"  split         train {run.train_sessions} / eval {run.eval_sessions}"
            f"  anchored={run.anchored}  complete-sessions={run.require_complete_sessions}"
        )
        print(f"  holdout       {run.holdout_start}..{run.holdout_end}")
        print()
        _print_windows(session, run.id)
    return 0


def cmd_holdout(run_id: int) -> int:
    """Take the final measurement, once, for a stored run.

    A holdout needs the report it concludes, which a later process does not
    hold — so the walk-forward is re-executed from the stored row to rebuild
    it. That is not a second experiment: the spec, the request, the snapshot
    and the split all come from the run, and `evaluate_and_persist_holdout`
    refuses the result unless every coordinate matches what was stored.

    A fitted run cannot be concluded this way. Its parameters were chosen by
    code the row does not contain, so there is nothing to rebuild the fitter
    from, and inventing one would put a number on the run that nothing in it
    explains.
    """
    with session_scope() as session:
        run = backtest_repo.get_run(session, run_id)
        if run is None:
            raise SystemExit(f"no backtest run {run_id}")
        if run.fitter_version:
            raise SystemExit(
                f"run {run_id} chose its parameters with {run.fitter_version}, which is "
                "code rather than data. Its holdout has to be taken in the same process "
                "as the walk-forward: `backtest run --with-holdout`"
            )
        if backtest_repo.holdout_of(session, run_id) is not None:
            raise SystemExit(
                f"run {run_id} already has its holdout. It is taken once, after every "
                f"choice has been made; see `backtest show --run {run_id}`"
            )

        stored = svc.evaluate_and_persist_holdout(session, run, _rebuild(session, run))
        session.commit()

        print(f"run #{run_id} holdout {stored.period_start}..{stored.period_end}")
        print(
            f"  return {_pct(_f(stored.total_return))}   MDD {_pct(_f(stored.max_drawdown))}"
            f"   Sharpe {_num(_f(stored.sharpe))}   trades {stored.trades}"
        )
    return 0


def _rebuild(session: Session, run: BacktestRun) -> svc.WalkForwardReport:
    """Re-execute a stored fixed run's walk-forward from its own row."""
    if run.holdout_start is None or run.holdout_end is None:
        raise SystemExit(f"run {run.id} reserved no holdout, so there is none to take")

    instrument = session.get(Instrument, run.instrument_id)
    if instrument is None:  # pragma: no cover - the foreign key guarantees this
        raise SystemExit(f"run {run.id} refers to an instrument that no longer exists")
    calendar = MarketCalendar(instrument.market)

    return svc.walk_forward(
        session,
        StrategySpec(
            definition=StrategyDefinition(
                kind=run.strategy_kind,
                version=run.strategy_version,
                params=run.strategy_params,
            )
        ),
        RunRequest(
            instrument_id=run.instrument_id,
            start=run.period_start,
            end=run.period_end,
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
        train_sessions=run.train_sessions,
        eval_sessions=run.eval_sessions,
        holdout_sessions=len(calendar.sessions_between(run.holdout_start, run.holdout_end)),
        anchored=run.anchored,
        data_snapshot_at=run.data_snapshot_at,
        require_complete_sessions=run.require_complete_sessions,
    )


def cmd_reproduce(run_id: int) -> int:
    """Re-run a stored backtest from its rows and report what differs."""
    with session_scope() as session:
        result = rs.reproduce(session, run_id)
        print(result.summary())

        if result.integrity:
            print("\n  integrity:")
            for finding in result.integrity:
                print(f"    - {finding}")

        for window in result.mismatches:
            print(f"    - {window.describe()}")

        if result.reproduced:
            print("\n  every stored measurement came back identical")
        return 0 if result.reproduced else 1


def _print_windows(session: Session, run_id: int) -> None:
    rows = backtest_repo.windows_of(session, run_id)
    if not rows:
        print("  no windows")
        return

    print(
        f"  {'window':<22} {'period':<24} {'return':>9} {'MDD':>9} {'Sharpe':>8} "
        f"{'trades':>7} {'caveats':>9}"
    )
    for row in rows:
        caveats = []
        if row.abstained:
            caveats.append(f"{row.abstained}a")
        if row.without_data:
            caveats.append(f"{row.without_data}d")
        if row.unfilled:
            caveats.append(f"{row.unfilled}u")
        label = f"{row.sample_type} #{row.window_index}"
        print(
            f"  {label:<22} {row.period_start}..{row.period_end} "
            f"{_pct(_f(row.total_return))} {_pct(_f(row.max_drawdown))} "
            f"{_num(_f(row.sharpe))} {row.trades:>7} {' '.join(caveats) or '-':>9}"
        )

    for sample in (SampleType.IN_SAMPLE, SampleType.OUT_OF_SAMPLE):
        returns = [_f(r.total_return) for r in rows if r.sample_type is sample]
        present = [r for r in returns if r is not None]
        if present:
            print(f"  {sample:<22} mean return {sum(present) / len(present):+8.2%}")


def _f(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Attach `backtest` and its verbs to the main parser."""
    backtest = sub.add_parser("backtest", help="run, inspect and reproduce backtests")
    verbs = backtest.add_subparsers(dest="verb", required=True)

    run = verbs.add_parser("run", help="walk forward and store the result")
    run.add_argument("--symbol", required=True)
    run.add_argument("--strategy", default="ma", choices=sorted(STRATEGIES))
    run.add_argument("--short", type=int, default=10)
    run.add_argument("--long", type=int, default=30)
    run.add_argument("--train", type=int, default=120)
    run.add_argument("--eval", type=int, default=60, dest="evaluate")
    run.add_argument("--holdout", type=int, default=60)
    run.add_argument("--anchored", action="store_true")
    run.add_argument("--commission-bps", default="5")
    run.add_argument("--slippage-bps", default="5")
    run.add_argument("--cash", default="10000000")
    run.add_argument(
        "--with-holdout",
        action="store_true",
        dest="with_holdout",
        help="take the final measurement in the same breath. Only when the choices "
        "are already made — a holdout seen on every run gets fitted by eye",
    )
    run.add_argument(
        "--allow-missing-sessions",
        action="store_true",
        dest="allow_missing",
        help="accept sessions with no bar, marked at the last printed price",
    )

    show = verbs.add_parser("show", help="what a stored run recorded")
    show.add_argument("--run", type=int, required=True, dest="run_id")

    holdout_cmd = verbs.add_parser("holdout", help="take a stored run's final measurement")
    holdout_cmd.add_argument("--run", type=int, required=True, dest="run_id")

    reproduce = verbs.add_parser("reproduce", help="re-run a stored backtest and compare")
    reproduce.add_argument("--run", type=int, required=True, dest="run_id")


def dispatch(args: argparse.Namespace) -> int:
    match args.verb:
        case "run":
            return cmd_run(
                args.symbol,
                args.strategy,
                args.short,
                args.long,
                args.train,
                args.evaluate,
                args.holdout,
                args.anchored,
                args.commission_bps,
                args.slippage_bps,
                args.cash,
                args.allow_missing,
                args.with_holdout,
            )
        case "show":
            return cmd_show(args.run_id)
        case "holdout":
            return cmd_holdout(args.run_id)
        case "reproduce":
            return cmd_reproduce(args.run_id)
        case _:  # pragma: no cover
            return 2
