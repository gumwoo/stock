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
from app.collectors.dart_fundamental import MAX_YEARS_BACK, DartFundamentalCollector
from app.collectors.krx_master import KrxMasterCollector
from app.collectors.naver_news import NaverNewsCollector
from app.collectors.quota import QuotaGuard
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
    "naver": NaverNewsCollector,
    "krx": KrxMasterCollector,
}

# How far back a collection reaches, in one vocabulary for every source that
# has a choice about it.
#
# One flag rather than a period string here and a year count there, because
# the thing that goes wrong is the two disagreeing. Samsung's ten-year backtest
# was run on ten years of prices and five years of DART filings — the default
# `years_back` — so for six and a half of those years the fundamental factor
# stood down and the rule could hold or exit but never enter. It returned
# +608%, and nothing in the collection, the run or the report said the two
# histories did not line up.
PERIODS: dict[str, int] = {"2y": 2, "5y": 5, "10y": 10, "max": MAX_YEARS_BACK}

# Sources whose range is decided by the source, not by us. SEC's companyfacts
# is the filer's entire XBRL history in a single document; there is no shorter
# request to make, so a period given here would be silently discarded.
FIXED_RANGE = frozenset({"sec", "naver"})


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


def cmd_quota() -> int:
    """What is left of each budget. Costs no quota, which is the point."""
    guard = QuotaGuard()
    print(f"{'quota':26s} {'spent':>8s} {'budget':>10s} {'left':>8s}  window / source")
    print("-" * 78)
    for quota, spent, allowed in guard.report():
        print(
            f"{quota.key:26s} {spent:>8,d} {allowed:>10,d} {max(0, allowed - spent):>8,d}"
            f"  {quota.window} [{quota.limit_source}]"
        )
    print()
    print("Budgets are a share of each published cap; a window is rolling, never a")
    print("calendar day, so no reset hour has to be known. See app/core/quota.py.")
    return 0


def cmd_collect(source: str, period: str | None = None, limit: int | None = None) -> int:
    factory = COLLECTORS.get(source)
    if factory is None:
        print(f"unknown source {source!r}; known: {', '.join(sorted(COLLECTORS))}")
        return 2

    if period is not None and source in FIXED_RANGE:
        print(
            f"{source} has no adjustable range — it fetches the filer's whole "
            f"history in one request. Drop --period."
        )
        return 2

    # DART indexes filings by business year, the price sources take yfinance's
    # period strings. Translated here rather than at the call site so the two
    # cannot be asked for different eras by accident.
    kwargs: dict[str, object] = {}
    if period is not None:
        kwargs = (
            {"years_back": PERIODS[period]} if source in {"dart", "krx"} else {"period": period}
        )

    # A deliberately tiny sweep, so the end-to-end check costs one call rather
    # than a pass over the whole listing master.
    if limit is not None:
        if source != "naver":
            print(f"--limit applies to naver only, not to {source}")
            return 2
        kwargs = {"max_instruments": limit, "max_pages": 1}

    with session_scope() as session:
        run = run_collector(factory(**kwargs), session)
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
    sub.add_parser("quota", help="how much of each API budget is left")

    collect = sub.add_parser("collect", help="run one collector")
    collect.add_argument("--source", required=True, choices=sorted(COLLECTORS))
    collect.add_argument(
        "--limit",
        type=int,
        help="naver only: sweep at most N instruments, one page each. For "
        "checking the pipe end to end without spending a day's budget",
    )
    collect.add_argument(
        "--period",
        choices=sorted(PERIODS),
        help="how far back to fetch. Applies to prices and to DART filings "
        "alike, because a backtest is only as long as the shorter of the two: "
        "ten years of prices against five of filings measures the technical "
        "half for the first five and reports it as the whole rule",
    )

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
        case "quota":
            return cmd_quota()
        case "collect":
            return cmd_collect(args.source, args.period, args.limit)
        case "backtest":
            return cli_backtest.dispatch(args)
        case "candles":
            return cmd_candles(args.symbol, Market(args.market), args.limit)
        case _:  # pragma: no cover
            parser.print_help()
            return 2


if __name__ == "__main__":
    sys.exit(main())
