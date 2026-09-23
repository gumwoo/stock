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
from functools import partial
from types import FrameType

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.collectors.base import CollectorError, run_collector
from app.collectors.dart_fundamental import DartFundamentalCollector
from app.collectors.naver_news import NaverNewsCollector
from app.collectors.quota import QuotaGuard
from app.collectors.sec_edgar import SecEdgarCollector
from app.collectors.yfinance_history import YFinanceHistoryCollector
from app.config import get_settings
from app.core import logging as logging_setup
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.db import advisory_lock, session_scope
from app.services import forward_service, llm_service, scoring_service

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


# Twice a day: an hour before the opening bell, and after the close. Written
# in the market's own timezone rather than UTC, which is what keeps NYSE from
# drifting an hour twice a year — it closes at 21:00 UTC in winter and 20:00 in
# summer. The times are margin, not precision; `has_closed` does the deciding.
_KR_PRE_OPEN = CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone="Asia/Seoul")
_KR_AFTER_CLOSE = CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone="Asia/Seoul")
# The daily loop the forward test lives on (Phase 4-8). Prices for the Korean
# session once it has closed and the 16:00 news sweep has had its ten
# minutes; then filings, scoring with its overlay, and the record of what
# earlier judgements turned into. One job, so the steps cannot run out of order.
_KR_DAILY_LOOP = CronTrigger(day_of_week="mon-fri", hour=16, minute=40, timezone="Asia/Seoul")
# US prices after the NYSE close, which is early morning in Seoul the next day.
_US_PRICES = CronTrigger(day_of_week="tue-sat", hour=7, minute=0, timezone="Asia/Seoul")
_SEC_WEEKLY = CronTrigger(day_of_week="sat", hour=8, minute=0, timezone="Asia/Seoul")

# After the morning sweep has landed and well before the 15:30 close. A signal
# is judged at the close and sees only news read by then; reading the morning's
# articles after the close would put them in tomorrow's signal instead.
_KR_READ_BEFORE_CLOSE = CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone="Asia/Seoul")


def _collect_korean_news(*, require_close: bool) -> None:
    """Sweep Korean news, if today is a day worth sweeping.

    The pre-open run wants a session today; the post-close run wants that
    session to be over. A holiday fails both, which saves the calls and keeps
    `collector_run` from filling with rows that look like ordinary weekdays.
    """
    calendar = MarketCalendar(Market.KR)
    now = utc_now()
    today = calendar.local_today(now)

    if not calendar.is_session(today):
        logger.info("naver_news: KRX is closed on %s", today)
        return
    if require_close and not calendar.has_closed(now):
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
        # Two business years, not five: new filings are recent ones. Not one:
        # the collector counts back from the calendar year, and this year's
        # annual report is not filed until next March, so one year would ask
        # only for a report that does not exist yet and still record SUCCESS
        # — which is what the fundamental freshness check reads.
        run_collector(DartFundamentalCollector(years_back=2), session)
        scored = scoring_service.score_all(session)
        logger.info("daily loop: scored %d", len(scored))
        logger.info(
            "daily loop: forward record +%d signal outcomes, %d candidates listed, "
            "+%d candidate outcomes",
            forward_service.evaluate_signals(session),
            forward_service.snapshot_candidates(session),
            forward_service.evaluate_candidates(session),
        )


def _us_prices() -> None:
    with session_scope() as session:
        run_collector(YFinanceHistoryCollector(period="1mo"), session)


def _sec_weekly() -> None:
    with session_scope() as session:
        run_collector(SecEdgarCollector(), session)


def _read_korean_news() -> None:
    """Judge undecided hits and read confirmed articles for tracked names.

    Tracked names only, and a bounded number of each, within the
    subscription's limits: this is the owner's Claude usage, spent unattended.
    """
    calendar = MarketCalendar(Market.KR)
    if not calendar.is_session(calendar.local_today(utc_now())):
        return
    limit = get_settings().llm_scheduled_limit
    with session_scope() as session:
        tracked = llm_service.tracked_ids(session)
        judged = llm_service.judge_pending(session, limit=limit, instrument_ids=tracked)
        logger.info("judge-news: %s", judged)
        if judged.stopped:
            return
        read = llm_service.read_confirmed(session, limit=limit, instrument_ids=tracked)
        logger.info("read-news: %s", read)


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
    for job_id, trigger, require_close in (
        ("naver_news_pre_open", _KR_PRE_OPEN, False),
        ("naver_news_after_close", _KR_AFTER_CLOSE, True),
    ):
        scheduler.add_job(
            guarded(job_id, partial(_collect_korean_news, require_close=require_close)),
            trigger,
            id=job_id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

    for job_id, trigger, fn in (
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

    if get_settings().llm_schedule_enabled:
        scheduler.add_job(
            guarded("news_reading_before_close", _read_korean_news),
            _KR_READ_BEFORE_CLOSE,
            id="news_reading_before_close",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

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
