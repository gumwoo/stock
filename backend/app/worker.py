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
from app.collectors.naver_news import NaverNewsCollector
from app.collectors.quota import QuotaGuard
from app.config import get_settings
from app.core import logging as logging_setup
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.db import advisory_lock, session_scope

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
