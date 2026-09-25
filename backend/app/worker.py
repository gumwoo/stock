"""Worker process — collectors, scoring and the scheduler.

Runs as its own container, separate from the API. Two reasons:

1. APScheduler inside a web app fires once per worker process. Scale uvicorn to
   two workers and every collector silently runs twice.
2. Collection and scoring are CPU- and IO-heavy in ways that have no business
   competing with request latency.

Every job additionally takes a Postgres advisory lock named after itself, so
even two worker containers cannot double-fire a job — the second simply declines.
"""

from __future__ import annotations

import logging
import signal
import sys
from collections.abc import Callable
from types import FrameType

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.collectors.base import CollectorError, run_collector
from app.collectors.dart_disclosure import DartDisclosureCollector
from app.collectors.dart_fundamental import DartFundamentalCollector
from app.collectors.kis_minute import (
    DEFAULT_BACKFILL_SESSIONS,
    KisIndexMinuteCollector,
    KisMinuteCollector,
)
from app.collectors.market_index import MarketIndexCollector
from app.collectors.naver_news import NaverNewsCollector
from app.collectors.quota import QuotaGuard
from app.collectors.sec_edgar import SecEdgarCollector
from app.collectors.yfinance_history import YFinanceHistoryCollector
from app.config import get_settings
from app.core import logging as logging_setup
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.db import advisory_lock, session_scope
from app.services import (
    forward_service,
    intraday_service,
    preopen_service,
    regime_service,
    scoring_service,
)

# Today's names at about four calls each, and the rest as backfill: a tenth of
# the day's KIS budget, and at one call every two seconds about half an hour,
# so the backfill fills the past over a week of evenings.
MINUTE_CALLS_PER_DAY = 1_000

logger = logging.getLogger("app.worker")


def guarded(job_name: str, fn: Callable[[], None]) -> Callable[[], None]:
    """Wrap a job so it only runs if it can take its advisory lock.

    Error handling mirrors the collector boundary. A `CollectorError` is logged
    and the scheduler carries on, because one source failing must not stop the
    others. Everything else — invariant violations, programming mistakes —
    propagates, so a correctness bug surfaces as a crash rather than as a log
    line nobody reads.
    """

    def run() -> None:
        with session_scope() as session, advisory_lock(session, job_name) as acquired:
            if not acquired:
                logger.info("%s: skipped, another worker holds the lock", job_name)
                return
            logger.info("%s: start", job_name)
            try:
                fn()
            except CollectorError:
                # Expected external failure; already recorded on collector_run.
                logger.warning("%s: source failed, continuing", job_name)
            else:
                logger.info("%s: done", job_name)

    run.__name__ = f"guarded_{job_name}"
    return run


# After the close. Written in the market's own timezone rather than UTC, which
# is what keeps NYSE from drifting an hour twice a year — it closes at 21:00
# UTC in winter and 20:00 in summer. The time is margin, not precision;
# `has_closed` does the deciding. 아침 스윕은 07:00 장전 체인 안으로 옮겼다.
_KR_AFTER_CLOSE = CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone="Asia/Seoul")
# The daily loop the forward test lives on (Phase 4-8). Prices for the Korean
# session once it has closed and the 16:00 news sweep has had its ten
# minutes; then filings, scoring with its overlay, and the record of what
# earlier judgements turned into. One job, so the steps cannot run out of order.
_KR_DAILY_LOOP = CronTrigger(day_of_week="mon-fri", hour=16, minute=40, timezone="Asia/Seoul")
# US prices after the NYSE close, which is early morning in Seoul the next day.
_US_PRICES = CronTrigger(day_of_week="tue-sat", hour=7, minute=0, timezone="Asia/Seoul")
_SEC_WEEKLY = CronTrigger(day_of_week="sat", hour=8, minute=0, timezone="Asia/Seoul")

# The day's minute bars, after the close and before the daily loop. A job of
# its own: a failure here must not take the proven daily loop down with it.
_KR_MINUTES = CronTrigger(day_of_week="mon-fri", hour=16, minute=20, timezone="Asia/Seoul")
# The index minute call returns only the last hundred or so minutes and no
# past day, so it is asked hourly through the session, and once after it.
_KR_INDEX_MINUTES = (
    CronTrigger(day_of_week="mon-fri", hour="10-15", minute=0, timezone="Asia/Seoul"),
    CronTrigger(day_of_week="mon-fri", hour=15, minute=40, timezone="Asia/Seoul"),
)
# PREOPEN_V2 아침 흐름 (`preopen_service`). 07:00 체인은 전체 스윕 → 풀 확정 →
# 검색 추세 → 사전 수집 → LLM을 한 작업 안에서 순서대로 돈다. 08:30 보충과
# 08:40 점수는 시각에 시작하지만, 앞 단계가 끝났는지는 풀의 단계 상태로
# 확인하고 기다린다. 08:50 목록은 개장 10분 전에 얼린다.
_KR_PREOPEN_MORNING = CronTrigger(day_of_week="mon-fri", hour=7, minute=0, timezone="Asia/Seoul")
_KR_PREOPEN_SUPPLEMENT = CronTrigger(
    day_of_week="mon-fri", hour=8, minute=30, timezone="Asia/Seoul"
)
_KR_PREOPEN_SCORES = CronTrigger(day_of_week="mon-fri", hour=8, minute=40, timezone="Asia/Seoul")
_KR_WATCHLIST = CronTrigger(day_of_week="mon-fri", hour=8, minute=50, timezone="Asia/Seoul")


def _collect_korean_news() -> None:
    """Sweep Korean news after the close, if today had a session that has ended.

    A holiday fails the check, which saves the calls and keeps `collector_run`
    from filling with rows that look like ordinary weekdays. 아침 스윕과 검색
    추세는 07:00 장전 체인(`preopen_service.run_morning`)이 한다.
    """
    calendar = MarketCalendar(Market.KR)
    now = utc_now()
    today = calendar.local_today(now)

    if not calendar.is_session(today):
        logger.info("naver_news: KRX is closed on %s", today)
        return
    if not calendar.has_closed(now):
        logger.info("naver_news: KRX session on %s has not finished", today)
        return

    # Buckets past every window answer nothing. Pruned here rather than on a
    # timer of its own: a table that only grows is a slow leak, and the tidying
    # belongs with the job that fills it.
    dropped = QuotaGuard().prune()
    if dropped:
        logger.info("quota ledger: pruned %d spent buckets", dropped)

    with session_scope() as session:
        run_collector(NaverNewsCollector(), session)
        # Event disclosures ride along. The morning chain's run is the one that
        # matters: last evening's filings are on record before today's close.
        run_collector(DartDisclosureCollector(), session)


def _daily_loop() -> None:
    """Prices, filings, scores and the forward record, after a Korean session.

    Holidays skip the whole loop: no session, no new bars, and a signal made
    anyway would restate yesterday's under a new row the forward test would
    have to explain away.
    """
    calendar = MarketCalendar(Market.KR)
    now = utc_now()
    if not calendar.is_session(calendar.local_today(now)) or not calendar.has_closed(now):
        logger.info("daily loop: no finished Korean session today")
        return
    with session_scope() as session:
        # A month of bars, not the default two years: the history is already
        # stored, and this only has to close the gap since the last run.
        run_collector(YFinanceHistoryCollector(period="1mo"), session)
        # The indexes the regime beside each signal is read from.
        run_collector(MarketIndexCollector(period="3mo"), session)
        # Two business years, not five: new filings are recent ones. Not one:
        # the collector counts back from the calendar year, and this year's
        # annual report is not filed until next March, so one year would ask
        # only for a report that does not exist yet and still record SUCCESS
        # — which is what the fundamental freshness check reads.
        run_collector(DartFundamentalCollector(years_back=2), session)
        run_collector(DartDisclosureCollector(), session)
        scored = scoring_service.score_all(session)
        logger.info("daily loop: scored %d", len(scored))
        # A regime held back because the day's index bar was late is filed
        # once the bar is in, rather than waiting for a hand-run backfill.
        logger.info("daily loop: regimes filed late %d", regime_service.backfill(session))
        logger.info(
            "daily loop: forward record +%d signal outcomes, %d candidates listed, "
            "+%d candidate outcomes",
            forward_service.evaluate_signals(session),
            forward_service.snapshot_candidates(session),
            forward_service.evaluate_candidates(session),
        )


def _kr_minutes() -> None:
    """Today's minute bars for the names in focus, then a bounded piece of their recent past."""
    calendar = MarketCalendar(Market.KR)
    now = utc_now()
    if not calendar.is_session(calendar.local_today(now)) or not calendar.has_closed(now):
        logger.info("kis minutes: no finished Korean session today")
        return
    with session_scope() as session:
        run_collector(
            KisMinuteCollector(
                instrument_ids=intraday_service.minute_targets(session),
                backfill_sessions=DEFAULT_BACKFILL_SESSIONS,
                max_calls=MINUTE_CALLS_PER_DAY,
            ),
            session,
        )
        # Only what came in whole is measured; the rest is recorded as such.
        logger.info("intraday analysis: %s", intraday_service.analyze(session))


def _kr_index_minutes() -> None:
    calendar = MarketCalendar(Market.KR)
    if not calendar.is_session(calendar.local_today(utc_now())):
        return
    with session_scope() as session:
        run_collector(KisIndexMinuteCollector(), session)


def _preopen_morning() -> None:
    with session_scope() as session:
        preopen_service.run_morning(session)


def _preopen_supplement() -> None:
    with session_scope() as session:
        preopen_service.run_supplement(session)


def _preopen_scores() -> None:
    with session_scope() as session:
        preopen_service.run_scores(session)


def _watchlist() -> None:
    with session_scope() as session:
        preopen_service.take_snapshot(session)


def _us_prices() -> None:
    with session_scope() as session:
        run_collector(YFinanceHistoryCollector(period="1mo"), session)
        run_collector(MarketIndexCollector(period="3mo"), session)


def _sec_weekly() -> None:
    with session_scope() as session:
        run_collector(SecEdgarCollector(), session)


def build_scheduler() -> BlockingScheduler:
    """Assemble the job schedule.

    Jobs are registered as later phases implement them. Phase 1 ships the
    process and its locking so that the boundary exists before there is traffic
    across it.
    """
    scheduler = BlockingScheduler(timezone="UTC")

    # `coalesce` and `misfire_grace_time` are not tidiness. This runs on a
    # laptop that sleeps: without them, a machine waking after three days fires
    # three times in a row, which is precisely the runaway the quota ledger
    # exists to prevent. The ledger would still hold, but two defences are
    # right for the one requirement that has no acceptable failure.
    for job_id, trigger, fn in (
        ("naver_news_after_close", _KR_AFTER_CLOSE, _collect_korean_news),
        # 개장 뒤에 늦게 깨어나면 체인이 스스로 하지 않는다(`run_morning`).
        ("preopen_morning", _KR_PREOPEN_MORNING, _preopen_morning),
        ("daily_loop_after_kr_close", _KR_DAILY_LOOP, _daily_loop),
        ("us_prices_after_close", _US_PRICES, _us_prices),
        ("sec_weekly", _SEC_WEEKLY, _sec_weekly),
    ):
        scheduler.add_job(
            guarded(job_id, fn),
            trigger,
            id=job_id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

    for job_id, trigger, fn in (
        ("preopen_supplement", _KR_PREOPEN_SUPPLEMENT, _preopen_supplement),
        ("preopen_scores", _KR_PREOPEN_SCORES, _preopen_scores),
    ):
        scheduler.add_job(
            guarded(job_id, fn),
            trigger,
            id=job_id,
            max_instances=1,
            coalesce=True,
            # 08:45가 기다림의 한계라, 그보다 늦은 발화는 의미가 없다.
            misfire_grace_time=600,
        )
    scheduler.add_job(
        guarded("watchlist_before_open", _watchlist),
        _KR_WATCHLIST,
        id="watchlist_before_open",
        max_instances=1,
        coalesce=True,
        # A list taken after the open is not a morning list: late is not at all.
        misfire_grace_time=300,
    )
    scheduler.add_job(
        guarded("kis_minutes_after_close", _kr_minutes),
        _KR_MINUTES,
        id="kis_minutes_after_close",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    for n, trigger in enumerate(_KR_INDEX_MINUTES):
        scheduler.add_job(
            guarded("kis_index_minutes", _kr_index_minutes),
            trigger,
            id=f"kis_index_minutes_{n}",
            max_instances=1,
            coalesce=True,
            # An hour late is the next run's job; a stale fire would only repeat it.
            misfire_grace_time=600,
        )

    # 장전 LLM 해석(07:00 체인과 08:30 보충)은 `LLM_SCHEDULE_ENABLED`가 켜져 있을
    # 때만 돈다. 꺼져 있으면 그 단계는 SKIPPED로 남고 나머지 아침은 그대로 간다.

    # Market.US waits for Threads and Reddit, whose caps are small enough that
    # reach has to follow the tracked set rather than the listing master.

    return scheduler


def main() -> int:
    settings = get_settings()
    logging_setup.configure(settings.log_level)
    diag = settings.diagnostics()
    logger.info("starting worker | env=%s | %s", settings.app_env, diag["summary"])

    scheduler = build_scheduler()

    def shutdown(signum: int, frame: FrameType | None) -> None:
        logger.info("signal %s received, shutting down", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    jobs = scheduler.get_jobs()
    if not jobs:
        logger.info("no jobs registered yet; worker idling")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
