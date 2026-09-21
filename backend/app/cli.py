"""Command line entry points for operating the system by hand.

Every collector is runnable individually. That matters during development for
the same reason it matters in production: when a score looks wrong, being able
to run one source in isolation and read its `collector_run` row is the
difference between a minute and an afternoon.

    python -m app.cli config          what is switched on, and what to set
    python -m app.cli seed            create the starting watchlist
    python -m app.cli collect --source yfinance
    python -m app.cli candles --symbol 005930
    python -m app.cli backtest run --symbol 005930
    python -m app.cli backtest show --run 1
    python -m app.cli backtest reproduce --run 1
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app import cli_backtest
from app.collectors.base import run_collector
from app.collectors.dart_fundamental import DartFundamentalCollector
from app.collectors.sec_edgar import SecEdgarCollector
from app.collectors.yfinance_history import FxRateCollector, YFinanceHistoryCollector
from app.config import get_settings
from app.core import logging as logging_setup
from app.core.calendar import Market
from app.core.clock import utc_now
from app.db import session_scope
from app.models import Interval
from app.repositories import candle_repo, instrument_repo
from app.seed import seed_watchlist

logger = logging.getLogger("app.cli")

COLLECTORS = {
    "yfinance": YFinanceHistoryCollector,
    "fx": FxRateCollector,
    "sec": SecEdgarCollector,
    "dart": DartFundamentalCollector,
}


def cmd_config() -> int:
    """Print the capability report — the same payload as /health/config."""
    diag = get_settings().diagnostics()
    print(diag["summary"])
    print()
    if diag["enabled"]:
        print("enabled:")
        for name in diag["enabled"]:
            print(f"  + {name}")
        print()
    print("disabled:")
    for item in diag["disabled"]:
        print(f"  - {item['name']:<20} set {', '.join(item['set_to_enable'])}")
        print(f"      {item['effect']}")
    return 0


def cmd_seed() -> int:
    with session_scope() as session:
        ids = seed_watchlist(session)
        print(f"seeded {len(ids)} instruments: {ids}")
        for instrument_id in ids:
            instrument = instrument_repo.get_by_id(session, instrument_id)
            symbol = instrument_repo.current_symbol(session, instrument_id)
            assert instrument is not None
            print(f"  {instrument_id}  {instrument.market}  {symbol}  {instrument.name}")
    return 0


def cmd_collect(source: str) -> int:
    factory = COLLECTORS.get(source)
    if factory is None:
        print(f"unknown source {source!r}; known: {', '.join(sorted(COLLECTORS))}")
        return 2

    with session_scope() as session:
        run = run_collector(factory(), session)
        print(f"{run.source}: {run.status} read={run.items_read} saved={run.items_saved}")
        if run.detail:
            print(f"  detail: {run.detail}")
        if run.error:
            print(f"  error:  {run.error}")
    return 0 if run.status.value in {"SUCCESS", "PARTIAL", "SKIPPED"} else 1


def cmd_candles(symbol: str, market: Market, limit: int) -> int:
    with session_scope() as session:
        instrument = instrument_repo.resolve_symbol(session, symbol, market, asof=utc_now().date())
        if instrument is None:
            print(f"no instrument for {symbol} in {market}; run `seed` first")
            return 1

        bars = candle_repo.history(session, instrument.instrument_id, Interval.DAY_1, limit=limit)
        total = candle_repo.count_for(session, instrument.instrument_id, Interval.DAY_1)
        print(f"{instrument.name} ({symbol}) — {total} daily bars stored")
        print(f"{'date':<12} {'open':>12} {'high':>12} {'low':>12} {'close':>12} {'volume':>14}")
        for bar in bars:
            print(
                f"{bar.ts.date().isoformat():<12} {bar.open:>12} {bar.high:>12} "
                f"{bar.low:>12} {bar.close:>12} {bar.volume:>14}"
            )
    return 0


def cmd_runs() -> int:
    """Latest run per collector, as JSON."""
    from sqlalchemy import select

    from app.models import CollectorRun

    with session_scope() as session:
        rows = session.execute(
            select(CollectorRun).order_by(CollectorRun.started_at.desc()).limit(20)
        ).scalars()
        out = [
            {
                "source": r.source,
                "status": r.status.value,
                "started_at": r.started_at.isoformat(),
                "read": r.items_read,
                "saved": r.items_saved,
                "detail": r.detail,
                "error": r.error,
            }
            for r in rows
        ]
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging_setup.configure()

    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("config", help="show enabled/disabled capabilities")
    sub.add_parser("seed", help="create the starting watchlist")
    sub.add_parser("runs", help="recent collector runs")

    collect = sub.add_parser("collect", help="run one collector")
    collect.add_argument("--source", required=True, choices=sorted(COLLECTORS))

    cli_backtest.register(sub)

    candles = sub.add_parser("candles", help="print stored daily bars")
    candles.add_argument("--symbol", required=True)
    candles.add_argument("--market", default="KR", choices=[m.value for m in Market])
    candles.add_argument("--limit", type=int, default=10)

    args = parser.parse_args(argv)

    match args.command:
        case "config":
            return cmd_config()
        case "seed":
            return cmd_seed()
        case "runs":
            return cmd_runs()
        case "collect":
            return cmd_collect(args.source)
        case "backtest":
            return cli_backtest.dispatch(args)
        case "candles":
            return cmd_candles(args.symbol, Market(args.market), args.limit)
        case _:  # pragma: no cover
            parser.print_help()
            return 2


if __name__ == "__main__":
    sys.exit(main())
