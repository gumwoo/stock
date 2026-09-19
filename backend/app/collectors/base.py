"""Collector foundation: isolation, run recording and rate limiting.

Two principles shape this module, and they pull in opposite directions on
purpose.

**External failure is isolated.** One source being down must not stop the other
nine. A collector that raises is caught here, recorded as FAILED, and the
pipeline continues. That is why this is the single place in the codebase where
a broad `except Exception` is permitted.

**Internal invariant violations are not caught.** Point-in-time leaks, execution
timing breaches and identity collisions propagate. A system that quietly
recovers from a correctness violation goes on to produce confident wrong
numbers, which is worse than stopping.

The `SKIPPED` status carries real weight. A collector with no credentials did
not fail — it was never configured. Conflating the two makes a fresh install
look like an outage and buries the one useful message: which value to fill in.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.models import CollectorRun, CollectorStatus

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CollectionResult:
    """What one collector run produced."""

    items_read: int = 0
    items_saved: int = 0
    partial: bool = False
    detail: str | None = None
    warnings: list[str] = field(default_factory=list)

    def status(self) -> CollectorStatus:
        return CollectorStatus.PARTIAL if self.partial else CollectorStatus.SUCCESS


class SkipCollection(Exception):  # noqa: N818 - control flow, not an error
    """Raised to decline a run without treating it as a failure.

    Deliberately not an error condition: missing credentials are a setup gap.
    The reason is surfaced verbatim so the dashboard can say exactly which
    environment variable to set.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CollectorError(Exception):
    """Base for typed collector failures.

    Subclasses per source, so that a Toss rate-limit and a DART parse error are
    distinguishable without string matching.
    """


class RateLimitedError(CollectorError):
    """The remote asked us to slow down."""


class UpstreamUnavailableError(CollectorError):
    """The remote is down, unreachable or returning garbage."""


class TokenBucket:
    """Simple rate limiter, one bucket per API group.

    Sized from the limits each provider publishes: Toss meters per endpoint
    group (Account 1/s, Market Data 15/s, Charts 20/s) and SEC allows 10/s,
    where exceeding it gets the IP blocked for ten minutes — so we sit at 8.
    """

    __slots__ = ("_capacity", "_rate", "_tokens", "_updated")

    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._rate = rate_per_second
        self._capacity = capacity if capacity is not None else rate_per_second
        self._tokens = self._capacity
        self._updated = time.monotonic()

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until `tokens` are available."""
        while True:
            now = time.monotonic()
            elapsed = now - self._updated
            self._updated = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

            if self._tokens >= tokens:
                self._tokens -= tokens
                return

            time.sleep((tokens - self._tokens) / self._rate)


class Collector(Protocol):
    """The contract every data source implements.

    A Protocol rather than an ABC: collectors share almost no behaviour, only a
    shape. Forcing them through a common base class would invite a god-parent
    full of conditionals for sources that work nothing alike.
    """

    name: str

    def is_configured(self) -> bool:
        """False when credentials are absent, which yields SKIPPED."""
        ...

    def collect(self, session: Session) -> CollectionResult:
        """Fetch, normalize and persist. Raise `CollectorError` on failure."""
        ...


class BaseCollector(ABC):
    """Convenience base providing `run()`; implementing `collect()` is yours."""

    name: str = "UNNAMED"

    def is_configured(self) -> bool:
        return True

    def skip_reason(self) -> str:
        return "not configured"

    @abstractmethod
    def collect(self, session: Session) -> CollectionResult:
        """Do the work. Raise `SkipCollection` or `CollectorError` as needed."""

    def run(self, session: Session) -> CollectorRun:
        """Execute, record the outcome, and never let failure escape.

        Returns the persisted `CollectorRun` so callers can inspect the result
        without re-querying. The run row is committed even on failure, since an
        unrecorded failure is exactly the kind that costs an afternoon later.
        """
        return run_collector(self, session)


def run_collector(collector: Collector, session: Session) -> CollectorRun:
    """Run one collector with isolation and full run recording."""
    started: datetime = utc_now()
    run = CollectorRun(
        source=collector.name,
        started_at=started,
        status=CollectorStatus.FAILED,
        items_read=0,
        items_saved=0,
    )

    if not collector.is_configured():
        reason = getattr(collector, "skip_reason", lambda: "not configured")()
        run.status = CollectorStatus.SKIPPED
        run.finished_at = utc_now()
        run.detail = reason
        session.add(run)
        session.commit()
        logger.info("%s: skipped (%s)", collector.name, reason)
        return run

    try:
        result = collector.collect(session)
    except SkipCollection as skip:
        run.status = CollectorStatus.SKIPPED
        run.detail = skip.reason
        logger.info("%s: skipped (%s)", collector.name, skip.reason)
    except CollectorError as exc:
        # A typed failure: the source misbehaved in a way we anticipated.
        run.status = CollectorStatus.FAILED
        run.error = f"{type(exc).__name__}: {exc}"
        logger.warning("%s: failed — %s", collector.name, exc)
    except Exception as exc:
        # The isolation boundary. Anything unexpected from the outside world
        # stops here so the other collectors still run.
        run.status = CollectorStatus.FAILED
        run.error = f"{type(exc).__name__}: {exc}"
        logger.exception("%s: unexpected failure", collector.name)
        del exc
    else:
        run.status = result.status()
        run.items_read = result.items_read
        run.items_saved = result.items_saved
        run.detail = result.detail
        if result.warnings:
            run.error = "; ".join(result.warnings)
        logger.info(
            "%s: %s read=%d saved=%d",
            collector.name,
            run.status,
            run.items_read,
            run.items_saved,
        )

    run.finished_at = utc_now()
    session.add(run)
    session.commit()
    return run
