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

from app.collectors.base import CollectorError
from app.config import get_settings
from app.core import logging as logging_setup
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


def build_scheduler() -> BlockingScheduler:
    """Assemble the job schedule.

    Jobs are registered as later phases implement them. Phase 1 ships the
    process and its locking so that the boundary exists before there is traffic
    across it.
    """
    scheduler = BlockingScheduler(timezone="UTC")

    # Phase 1 registers no jobs yet: the collectors land in the next step.
    # Keeping the process here, with its lock discipline already in place,
    # means jobs get added to a structure that is already correct.

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
