"""Command line entry points for operating the system by hand.

Every collector is runnable individually. That matters during development for
the same reason it matters in production: when a score looks wrong, being able
to run one source in isolation and read its `collector_run` row is the
difference between a minute and an afternoon.

    python -m app.cli config          what is switched on, and what to set
    python -m app.cli seed            create the starting watchlist
    python -m app.cli collect --source yfinance
    python -m app.cli candles --symbol 005930
    python -m app.cli discover        untracked names whose news surged
    python -m app.cli promote --top 3 fetch their data, then track them
    python -m app.cli backtest run --symbol 005930
    python -m app.cli backtest show --run 1
    python -m app.cli backtest reproduce --run 1
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import cli_backtest
from app.collectors.base import run_collector
from app.collectors.dart_disclosure import DartDisclosureCollector
from app.collectors.dart_fundamental import MAX_YEARS_BACK, DartFundamentalCollector
from app.collectors.kis_minute import KisIndexMinuteCollector, KisMinuteCollector
from app.collectors.krx_master import KrxMasterCollector
from app.collectors.market_index import INDEXES, MarketIndexCollector
from app.collectors.naver_datalab import NaverDataLabCollector
from app.collectors.naver_news import RULE_VERSION as NEWS_RULE_VERSION
from app.collectors.naver_news import NaverNewsCollector, rejudge_hits
from app.collectors.quota import QuotaGuard
from app.collectors.sec_edgar import SecEdgarCollector
from app.collectors.yfinance_history import FxRateCollector, YFinanceHistoryCollector
from app.config import get_settings
from app.core import logging as logging_setup
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.core.types import Freshness
from app.db import session_scope
from app.models import Interval
from app.repositories import candle_repo, instrument_repo, news_repo
from app.scoring.review import DayStats
from app.seed import seed_watchlist
from app.services import (
    discovery_service,
    forward_service,
    intraday_service,
    llm_service,
    overlay_service,
    preopen_service,
    promotion_service,
    regime_service,
    review_service,
)
from app.services.discovery_service import Candidate, Discovery

logger = logging.getLogger("app.cli")

COLLECTORS = {
    "yfinance": YFinanceHistoryCollector,
    "fx": FxRateCollector,
    "sec": SecEdgarCollector,
    "dart": DartFundamentalCollector,
    "disclosure": DartDisclosureCollector,
    "naver": NaverNewsCollector,
    "krx": KrxMasterCollector,
    "index": MarketIndexCollector,
    "datalab": NaverDataLabCollector,
    "index_minute": KisIndexMinuteCollector,
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
FIXED_RANGE = frozenset({"sec", "naver", "disclosure", "datalab", "index_minute"})


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


def cmd_collect(
    source: str,
    period: str | None = None,
    limit: int | None = None,
    only: str | None = None,
) -> int:
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

    # Named companies only, one page each: for checking a rule change on the
    # companies that prompted it before paying for the whole market.
    if only is not None:
        if source != "naver":
            print(f"--only applies to naver only, not to {source}")
            return 2
        names = [n.strip() for n in only.split(",") if n.strip()]
        if not names:
            print("--only needs at least one company name")
            return 2
        kwargs = {"only": names, "max_pages": 1}

    with session_scope() as session:
        if source == "datalab":
            # The names in focus, as the morning job asks for them.
            kwargs = {"instrument_ids": llm_service.focus_ids(session)}
        run = run_collector(factory(**kwargs), session)
        print(f"{run.source}: {run.status} read={run.items_read} saved={run.items_saved}")
        if run.detail:
            print(f"  detail: {run.detail}")
        if run.error:
            print(f"  error:  {run.error}")
    return 0 if run.status.value in {"SUCCESS", "PARTIAL", "SKIPPED"} else 1


def cmd_rejudge_news() -> int:
    """Re-decide stored query hits under the current relevance rule.

    Costs no API call. Prints what moved and whether the mention table still
    agrees with the confirmed hits, which it must.
    """
    with session_scope() as session:
        result = rejudge_hits(session)
        counts = news_repo.hit_counts(session)
        missing, orphaned = news_repo.projection_drift(session)

    print(
        f"re-judged {result.judged}: {result.confirmed} confirmed, "
        f"{result.pending} pending, {result.rejected} rejected"
    )
    print(f"mentions: +{result.mentions_added} -{result.mentions_removed}")
    if result.skipped:
        print(f"skipped {result.skipped} whose company is no longer in the Korean universe")
    if result.unread:
        print(
            f"left {result.unread} stored before hits kept their snippet; the next sweep re-reads them"
        )
    print("hits now: " + ", ".join(f"{k.value} {v}" for k, v in sorted(counts.items())))
    print(
        f"projection drift: {missing} confirmed without a mention, {orphaned} mentions unconfirmed"
    )
    return 0 if (missing, orphaned) == (0, 0) else 1


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


def _discover_args(args: argparse.Namespace, *, top: int, min_recent: int) -> Discovery:
    with session_scope() as session:
        return discovery_service.discover(
            session,
            asof=datetime.fromisoformat(args.asof) if args.asof else None,
            window=timedelta(hours=args.window_hours),
            baseline=timedelta(days=args.baseline_days),
            top=top,
            min_recent=min_recent,
        )


def _print_discovery(found: Discovery, candidates: list[Candidate] | None = None) -> None:
    print(
        f"as of {found.asof.isoformat(timespec='minutes')}: news {found.freshness.value} "
        f"(newest article {found.newest_article.isoformat(timespec='minutes') if found.newest_article else 'none'})"
    )
    reach = (
        found.coverage_start.isoformat(timespec="minutes") if found.coverage_start else "nothing"
    )
    print(f"window {found.window}, baseline {found.baseline}; collection reaches back to {reach}")
    if found.freshness.value != "FRESH":
        print("  news is not flowing: a list made now describes an old picture")
    print(
        f"{found.considered} untracked names considered; {found.unmeasured} with recent "
        f"mentions left unranked because too little of their news was read"
    )
    print(
        f"{'#':>3} {'name':<16} {'symbol':<8} {'board':<7} {'recent':>6} {'days':>5} "
        f"{'before':>6} {'days':>5} {'expected':>8} {'score':>6}"
    )
    for n, c in enumerate(found.candidates if candidates is None else candidates, 1):
        print(
            f"{n:>3} {c.name:<16} {c.symbol or '-':<8} "
            f"{c.listing.value if c.listing else '-':<7} {c.recent:>6} {c.recent_days:>5.2f} "
            f"{c.baseline:>6} {c.baseline_days:>5.2f} {c.expected:>8.1f} {c.score:>6.2f}"
        )


def cmd_discover(args: argparse.Namespace) -> int:
    """Untracked names whose news surged. Reads only; costs no API call."""
    _print_discovery(_discover_args(args, top=args.top, min_recent=args.min_recent))
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """Fetch prices and filings for candidates, then track those whose prices arrived.

    Spends quota: yfinance once per name, DART about six calls per name.
    """
    wanted = {s.strip() for s in (args.symbols or "").split(",") if s.strip()}
    if not wanted and not args.top:
        print("say which: --symbols 123456,234567 or --top N")
        return 2
    found = _discover_args(args, top=10_000, min_recent=1 if wanted else args.min_recent)
    if wanted:
        chosen = [c for c in found.candidates if c.symbol in wanted]
        missing = wanted - {c.symbol for c in chosen}
        if missing:
            print(f"not among untracked names with recent mentions: {', '.join(sorted(missing))}")
            return 2
    else:
        chosen = found.candidates[: args.top]
    # Asked about a past moment, the list can hold names promoted since. They
    # are tracked now; fetching their data again only to be refused is waste.
    with session_scope() as session:
        tracked_now = {
            c.instrument_id
            for c in chosen
            if (i := instrument_repo.get_by_id(session, c.instrument_id)) is not None and i.tracked
        }
    if tracked_now:
        print(f"skipping {len(tracked_now)} already tracked now")
        chosen = [c for c in chosen if c.instrument_id not in tracked_now]
    _print_discovery(found, chosen)
    with session_scope() as session:
        outcomes = promotion_service.promote(session, found, chosen)
    for o in outcomes:
        mark = "+" if o.promoted else "-"
        print(f"  {mark} {o.name}: {o.reason}; {o.candle_bars} bars, {o.fundamental_facts} facts")
    return 0 if any(o.promoted for o in outcomes) else 1


def _print_llm_report(report: llm_service.LlmRunReport) -> None:
    print(
        f"{report.purpose}: asked about {report.asked} in {report.calls} calls, wrote {report.written}"
        + (
            f", {report.malformed_batches} malformed batches skipped"
            if report.malformed_batches
            else ""
        )
        + (f", {report.unanswered} left unanswered" if report.unanswered else "")
    )
    if report.counts:
        print("  " + ", ".join(f"{k} {v}" for k, v in sorted(report.counts.items())))
    print(f"  tokens in/out {report.input_tokens:,}/{report.output_tokens:,}")
    if report.five_hour_utilization is not None or report.seven_day_utilization is not None:
        five = report.five_hour_utilization
        seven = report.seven_day_utilization
        print(
            "  subscription usage after the last call: "
            f"5h {'-' if five is None else f'{five:.0%}'}, 7d {'-' if seven is None else f'{seven:.0%}'}"
        )
    if report.stopped:
        print(f"  stopped early: {report.stopped}")


def _scope_ids(session: Session, scope: str) -> list[int] | None:
    """The instruments a model run covers: focus (tracked and candidates), tracked, or all."""
    if scope == "all":
        return None
    if scope == "tracked":
        return llm_service.tracked_ids(session)
    return llm_service.focus_ids(session)


def cmd_judge_news(limit: int, scope: str) -> int:
    """PENDING hits the rule could not settle, asked of the model. Uses the subscription."""
    with session_scope() as session:
        ids = _scope_ids(session, scope)
        report = llm_service.judge_pending(session, limit=limit, instrument_ids=ids)
        missing, orphaned = news_repo.projection_drift(session)
    _print_llm_report(report)
    print(
        f"projection drift: {missing} confirmed without a mention, {orphaned} mentions unconfirmed"
    )
    return 0 if (missing, orphaned) == (0, 0) else 1


def cmd_read_news(limit: int, scope: str) -> int:
    """CONFIRMED articles read for direction, event and intensity. Uses the subscription."""
    with session_scope() as session:
        ids = _scope_ids(session, scope)
        report = llm_service.read_confirmed(session, limit=limit, instrument_ids=ids)
    _print_llm_report(report)
    return 0


def cmd_audit_rules(sample: int, report_only: bool) -> int:
    """The model's second opinion on a sample of the rule's confirmations. Uses the subscription."""
    with session_scope() as session:
        if not report_only:
            _print_llm_report(llm_service.audit_rules(session, sample=sample))
        tables = llm_service.audit_report(session, rule_version=NEWS_RULE_VERSION)
    print(f"rule v{NEWS_RULE_VERSION} confirmations, as the model judged them:")
    for kind, rows in tables.items():
        print(f"  by {kind}:")
        for key, a in rows.items():
            share = "-" if a.precision is None else f"{a.precision:.0%}"
            print(
                f"    {key:<24} audited {a.audited:>4}  agree {a.confirmed:>4}  "
                f"reject {a.rejected:>4}  unsure {a.unsure:>4}  agreement {share}"
            )
    return 0


def cmd_overlay(asof: str | None, symbol: str | None) -> int:
    """The news-event overlay for tracked names at a moment. Reads only."""
    moment = datetime.fromisoformat(asof) if asof else utc_now()
    with session_scope() as session:
        tracked = instrument_repo.list_active(session, asof=moment.date(), tracked=True)
        if symbol:
            tracked = [
                i
                for i in tracked
                if instrument_repo.current_symbol(session, i.instrument_id) == symbol
            ]
        results = overlay_service.overlays_at(
            session, asof=moment, instrument_ids=[i.instrument_id for i in tracked]
        )
        names = {i.instrument_id: i.name for i in tracked}
    if not results:
        print("no tracked instrument matched")
        return 1
    any_result = next(iter(results.values()))
    print(
        f"as of {any_result.asof.isoformat(timespec='minutes')}; "
        f"readings by {any_result.model} prompt v{any_result.prompt_version}; "
        f"overlay v{any_result.params.version}, at most +/-{any_result.params.max_points:g} points"
    )
    for instrument_id, r in sorted(results.items(), key=lambda kv: -abs(kv[1].overlay.points)):
        o = r.overlay
        print(
            f"{names[instrument_id]:<16} {o.points:+6.2f} pts  {len(o.clusters)} events from "
            f"{o.readings_used} readings, {r.unread_articles} confirmed articles unread"
            + ("" if r.news_freshness is Freshness.FRESH else f"  [news {r.news_freshness.value}]")
        )
        for c in o.clusters[:3]:
            print(
                f"    {c.event_type:<18} {c.first_at:%m-%d %H:%M} x{c.articles:<3} "
                f"s={c.sentiment:+.2f} i={c.intensity:.2f} decay={c.decay:.2f} -> {c.contribution:+.3f}  "
                f"{c.title[:40]}"
            )
    return 0


def cmd_regime(asof: str | None, backfill: bool) -> int:
    """The market regime of each index at a moment; optionally file it for past signals."""
    moment = datetime.fromisoformat(asof) if asof else utc_now()
    with session_scope() as session:
        for code in INDEXES:
            r = regime_service.regime_at(session, code, moment)
            print(
                f"{code:<6} {r.label:<9} close {_fmt(r.close, ',.2f'):>10}  "
                f"vs 200d {_fmt(r.trend_gap, '+.1%'):>7}  20d {_fmt(r.return_20d, '+.1%'):>7}  "
                f"vol {_fmt(r.volatility, '.1%'):>6} (rank {_fmt(r.volatility_rank, '.0%')})"
            )
        for market in (Market.KR, Market.US):
            share, names = regime_service.breadth_at(session, market, moment)
            print(f"breadth {market.value}: {_fmt(share, '.0%')} of {names} tracked names")
        if backfill:
            print(f"filed a regime beside {regime_service.backfill(session)} signals")
    return 0


def _stat(d: DayStats) -> str:
    t = d.t
    return (
        f"days={d.days:<3} mean {_fmt(d.mean, '+.2f'):>6}  t {'-' if t is None else f'{t:.2f}':>5}"
    )


def cmd_review() -> int:
    """The forward record against its review gates, and the overlay decision rule. Reads only."""
    with session_scope() as session:
        rev = review_service.review(session)
    print(
        "Review gates (rules fixed in app/scoring/review.py before the record had data). "
        "Excess returns in %, averaged within each entry day first."
    )
    print(f"first entry: {rev.first_entry or 'none yet'}")
    for g in rev.gates:
        when = (
            "reached"
            if g.reached
            else f"earliest {g.earliest:%Y-%m-%d} if every session is recorded"
        )
        print(
            f"  {g.gate.name:<13} {g.days:>3}/{g.gate.days} entry days at {g.gate.horizon}d — {when}"
        )
    if rev.spread is not None:
        print()
        print("overlay, 5-session excess")
        print(f"  good news  {_stat(rev.spread.good)}")
        print(f"  bad news   {_stat(rev.spread.bad)}")
        t = rev.spread.t
        print(
            f"  difference {_fmt(rev.spread.difference, '+.2f')}  "
            f"t {'-' if t is None else f'{t:.2f}'}"
        )
    if rev.verdict is not None:
        print()
        state = "passed" if rev.verdict.passed else ("failed" if rev.verdict.ready else "not ready")
        print(f"may the overlay be proposed to change actions? {state}")
        for reason in rev.verdict.reasons:
            print(f"  - {reason}")
        if rev.verdict.passed:
            print(
                "  next: a new strategy version; count its action distribution before "
                "accepting any threshold; evaluate without touching the holdout"
            )
    print()
    print("half-life check: excess in the event's direction, by event age (exploratory)")
    if not rev.half_life:
        print("  nothing measured yet")
    for label, d in rev.half_life.items():
        print(f"  {label:<12} {_stat(d)}")
    return 0


def cmd_minutes(backfill: int, max_calls: int) -> int:
    """Minute bars for the names in focus: today if closed, and `backfill` sessions before."""
    with session_scope() as session:
        run = run_collector(
            KisMinuteCollector(
                instrument_ids=intraday_service.minute_targets(session),
                backfill_sessions=backfill,
                max_calls=max_calls,
            ),
            session,
        )
        print(f"{run.source}: {run.status} calls={run.items_read} bars={run.items_saved}")
        if run.detail:
            print(f"  detail: {run.detail}")
        if run.error:
            print(f"  error:  {run.error}")
    return 0 if run.status.value in {"SUCCESS", "PARTIAL", "SKIPPED"} else 1


def cmd_watchlist(take: bool) -> int:
    """The newest morning watchlist; `--take` freezes today's if none exists. Reads otherwise."""
    from sqlalchemy import select

    from app.models import Instrument, WatchlistMember, WatchlistSnapshot

    with session_scope() as session:
        if take:
            made = preopen_service.take_snapshot(session)
            print(
                "took today's snapshot"
                if made
                else "no snapshot taken (not a session, or one exists)"
            )
        snap = session.execute(
            select(WatchlistSnapshot).order_by(WatchlistSnapshot.created_at.desc()).limit(1)
        ).scalar_one_or_none()
        if snap is None:
            print("no watchlist yet")
            return 0
        print(
            f"{snap.session_date} {snap.strategy_version} as of {snap.asof:%Y-%m-%d %H:%M}Z: "
            f"{snap.pool} considered, {snap.left_out} left out"
        )
        print(f"  inputs: {snap.inputs}")
        rows = session.execute(
            select(WatchlistMember, Instrument.name)
            .join(Instrument, Instrument.instrument_id == WatchlistMember.instrument_id)
            .where(WatchlistMember.snapshot_id == snap.id)
            .order_by(WatchlistMember.rank)
        ).all()
        for m, name in rows:
            print(
                f"  {m.rank:>2}. {name:<16} overlay {_fmt(m.overlay_points, '+.1f'):>5}  "
                f"search {_fmt(m.attention_surge, '.2f'):>5}  {', '.join(m.reasons)}"
            )
    return 0


def cmd_preopen(action: str, asof: str | None) -> int:
    """장전 후보 풀. `status`와 `dry-run`은 읽기만 한다. 나머지는 그 단계를 지금 돌린다."""
    from datetime import datetime

    with session_scope() as session:
        if action == "dry-run":
            moment = datetime.fromisoformat(asof) if asof else utc_now()
            result = preopen_service.dry_run(session, asof=moment)
            print("참고용 재계산 — 그때는 07:00 사전 수집·LLM과 08:30 보충이 없었다")
            for key, value in result.items():
                if key != "names":
                    print(f"  {key}: {value}")
            for rank, name, reasons in result["names"]:  # type: ignore[attr-defined]
                print(f"  {rank:>2}. {name:<16} {', '.join(reasons)}")
            session.rollback()
            return 0
        if action == "morning":
            preopen_service.run_morning(session)
        elif action == "supplement":
            preopen_service.run_supplement(session)
        elif action == "scores":
            preopen_service.run_scores(session)
        day = MarketCalendar(Market.KR).local_today(utc_now())
        pool = preopen_service.pool_for(session, day)
        if pool is None:
            print(f"no pool for {day}")
            return 0
        print(f"{pool.session_date} {pool.status}: {pool.pool_count} names, frozen {pool.asof}")
        for stage, entry in pool.stages.items():
            print(f"  {stage:<15} {entry}")
        counts: dict[str, int] = {}
        for m in preopen_service.members_of(session, pool):
            key = m.prefetch_status or "NONE"
            counts[key] = counts.get(key, 0) + 1
        print(f"  prefetch: {counts}")
    return 0


def cmd_intraday(analyze: bool) -> int:
    """What the minute bars say: the usual day, and the morning lists against their questions."""
    with session_scope() as session:
        if analyze:
            print(f"analysed: {intraday_service.analyze(session)}")
        rep = intraday_service.report(session)
    b = rep.baseline
    print(
        f"Baseline — every whole day on record, chosen or not: {b.name_days} name-days over {b.days} days."
    )
    print(
        "It describes these names, not our choices. MFE/MAE are hindsight, not returns anyone took."
    )
    print(f"  median MFE {_fmt(b.median_mfe, '+.2f')}%  median MAE {_fmt(b.median_mae, '+.2f')}%")
    print("  half-hour   volume share   mean |return|   share of days' highs")
    for start in b.volume_share:
        print(
            f"  {start}      {_fmt(b.volume_share.get(start), '.1%'):>8}       "
            f"{_fmt(b.abs_return.get(start), '.2f'):>6}%        "
            f"{_fmt(b.high_bucket_share.get(start, 0.0), '.0%'):>5}"
        )
    print("  (09:00 holds the opening auction's volume, 15:00 the closing auction's)")
    print()
    print(
        f"Morning lists: {rep.watchlist_days} days, {rep.members} name-days measured, "
        f"{rep.missing} not yet whole. Questions fixed in app/scoring/intraday_review.py; "
        "20 days to read, the first 60 to decide."
    )
    for r in rep.results:
        t = "-" if r.t is None else f"{r.t:.2f}"
        print(
            f"  {r.key} {r.text:<44} days={r.days:<3} mean {_fmt(r.mean, '+.2f'):>6}  t {t:>5}  {r.state}"
        )
    return 0


def cmd_forward_run() -> int:
    """Add what has become measurable, and take today's candidate list."""
    with session_scope() as session:
        signals = forward_service.evaluate_signals(session)
        listed = forward_service.snapshot_candidates(session)
        candidates = forward_service.evaluate_candidates(session)
    print(
        f"signal outcomes +{signals}, candidates listed {listed}, candidate outcomes +{candidates}"
    )
    return 0


def _fmt(value: float | None, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def cmd_forward() -> int:
    """What the record says so far. Reads only."""
    with session_scope() as session:
        rep = forward_service.report(session)
    print(
        f"{rep.signals_recorded} judgements on record, {rep.snapshots_recorded} candidate listings. "
        "Returns in %; excess is against the same day's judged names in that market."
    )
    print("A few weeks are a handful of independent days: read counts before means.")
    for title, table in (
        ("by action", rep.by_action),
        ("by news overlay", rep.by_overlay),
        ("by market regime", rep.by_regime),
        ("by search attention", rep.by_attention),
        ("candidates", rep.candidates),
        ("candidates by market regime", rep.candidates_by_regime),
        ("candidates by search attention", rep.candidates_by_attention),
    ):
        print()
        print(title)
        if not table:
            print("  nothing measured yet")
            continue
        for horizon in sorted(table):
            for label, st in sorted(table[horizon].items()):
                print(
                    f"  {horizon:>2}d  {label:<14} n={st.n:<4} days={st.days:<3} "
                    f"mean {_fmt(st.mean, '+.2f'):>6}  median {_fmt(st.median, '+.2f'):>6}  "
                    f"hit {_fmt(st.hit_rate, '.0%'):>4}  excess {_fmt(st.mean_excess, '+.2f'):>6}"
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
    sub.add_parser(
        "rejudge-news", help="re-decide stored news hits under the current rule; no API calls"
    )

    collect = sub.add_parser("collect", help="run one collector")
    collect.add_argument("--source", required=True, choices=sorted(COLLECTORS))
    collect.add_argument(
        "--limit",
        type=int,
        help="naver only: sweep at most N instruments, one page each. For "
        "checking the pipe end to end without spending a day's budget",
    )
    collect.add_argument(
        "--only",
        help="naver only: comma-separated company names to sweep, one page each",
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

    for name, helptext in (
        ("discover", "untracked names whose news surged; reads only, no API calls"),
        ("promote", "fetch prices and filings for candidates, then track them; spends quota"),
    ):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument("--asof", help="ISO time to ask about; default now")
        cmd.add_argument(
            "--window-hours",
            type=int,
            default=int(discovery_service.DEFAULT_WINDOW.total_seconds() // 3600),
        )
        cmd.add_argument(
            "--baseline-days", type=int, default=discovery_service.DEFAULT_BASELINE.days
        )
        cmd.add_argument("--min-recent", type=int, default=discovery_service.MIN_RECENT)
        if name == "discover":
            cmd.add_argument("--top", type=int, default=discovery_service.DEFAULT_TOP)
        else:
            cmd.add_argument("--top", type=int, help="promote the top N of the discovery")
            cmd.add_argument("--symbols", help="promote these, comma-separated")

    judge = sub.add_parser(
        "judge-news",
        help="ask the model about PENDING news hits; uses the Claude subscription",
    )
    judge.add_argument("--limit", type=int, default=100)
    judge.add_argument(
        "--scope",
        choices=("focus", "tracked", "all"),
        default="focus",
        help="focus = tracked names and recent candidates (default)",
    )
    read = sub.add_parser(
        "read-news",
        help="read CONFIRMED articles for sentiment and event; uses the Claude subscription",
    )
    read.add_argument("--limit", type=int, default=100)
    read.add_argument(
        "--scope",
        choices=("focus", "tracked", "all"),
        default="focus",
        help="focus = tracked names and recent candidates (default)",
    )
    audit = sub.add_parser(
        "audit-rules",
        help="ask the model about a random sample of the rule's confirmations; "
        "records its answers, changes no verdict; uses the Claude subscription",
    )
    audit.add_argument("--sample", type=int, default=50)
    audit.add_argument(
        "--report-only", action="store_true", help="print the agreement so far; no calls"
    )

    overlay = sub.add_parser(
        "overlay", help="news-event overlay for tracked names; reads only, no API calls"
    )
    overlay.add_argument("--asof", help="ISO time to ask about; default now")
    overlay.add_argument("--symbol")

    regime = sub.add_parser(
        "regime", help="market regime from the index closes; reads only unless --backfill"
    )
    regime.add_argument("--asof", help="ISO time to ask about; default now")
    regime.add_argument(
        "--backfill", action="store_true", help="file a regime beside every signal that has none"
    )

    minutes = sub.add_parser(
        "minutes", help="KIS one-minute bars for the names in focus; calls KIS within its quota"
    )
    minutes.add_argument("--backfill", type=int, default=0, help="past sessions to fill")
    minutes.add_argument("--max-calls", type=int, default=200)

    pre = sub.add_parser(
        "preopen",
        help="the morning pool (PREOPEN_V2); status and dry-run read only, the rest run that stage",
    )
    pre.add_argument("action", choices=["status", "morning", "supplement", "scores", "dry-run"])
    pre.add_argument("--asof", help="dry-run moment, ISO with offset (default: now)")

    watch = sub.add_parser("watchlist", help="the newest morning watchlist; reads only")
    watch.add_argument("--take", action="store_true", help="freeze today's if none exists")

    intra = sub.add_parser("intraday", help="what the minute bars say; reads only unless --analyze")
    intra.add_argument("--analyze", action="store_true", help="summarise days not yet summarised")

    sub.add_parser("forward", help="the forward-test record so far; reads only")
    sub.add_parser(
        "review", help="the forward record against its review gates and decision rule; reads only"
    )
    sub.add_parser(
        "forward-run",
        help="add measurable outcomes and take today's candidate list; fetches candidate prices",
    )

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
            return cmd_collect(args.source, args.period, args.limit, args.only)
        case "rejudge-news":
            return cmd_rejudge_news()
        case "backtest":
            return cli_backtest.dispatch(args)
        case "discover":
            return cmd_discover(args)
        case "promote":
            return cmd_promote(args)
        case "judge-news":
            return cmd_judge_news(args.limit, args.scope)
        case "read-news":
            return cmd_read_news(args.limit, args.scope)
        case "audit-rules":
            return cmd_audit_rules(args.sample, args.report_only)
        case "overlay":
            return cmd_overlay(args.asof, args.symbol)
        case "regime":
            return cmd_regime(args.asof, args.backfill)
        case "review":
            return cmd_review()
        case "minutes":
            return cmd_minutes(args.backfill, args.max_calls)
        case "watchlist":
            return cmd_watchlist(args.take)
        case "preopen":
            return cmd_preopen(args.action, args.asof)
        case "intraday":
            return cmd_intraday(args.analyze)
        case "forward":
            return cmd_forward()
        case "forward-run":
            return cmd_forward_run()
        case "candles":
            return cmd_candles(args.symbol, Market(args.market), args.limit)
        case _:  # pragma: no cover
            parser.print_help()
            return 2


if __name__ == "__main__":
    sys.exit(main())
