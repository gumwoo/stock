"""Collector foundation: isolation, run recording and rate limiting.

Two principles shape this module, and they pull in opposite directions on
purpose.

**External failure is isolated.** One source being down must not stop the other
nine. Network errors, timeouts and malformed responses are recorded as FAILED
and the pipeline continues.

**Internal invariant violations are not caught.** Point-in-time leaks,
execution-timing breaches, identity collisions — and ordinary programming
mistakes such as `AttributeError` or `KeyError` — propagate. A system that
quietly logs a bug as though it were an outage goes on producing confident
wrong numbers, and the run log stops meaning what it says.

Making that real means **not** catching `Exception` here. Only `CollectorError`
and a narrow set of genuinely external failure types are contained. Collectors
are responsible for wrapping their own outbound calls:

    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        raise UpstreamUnavailableError(...) from exc

Anything a collector does not wrap and does not expect is a defect, and defects
should stop the work rather than be filed under "the API was down".

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


# Failures that can only have come from outside the process. Kept short on
# purpose: every addition is a way for a bug to be misfiled as an outage.
# `OSError` covers socket, DNS and connection errors, and most HTTP client
# libraries derive their transport errors from it or wrap them.
EXTERNAL_FAILURES: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    ConnectionError,
)


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
    except EXTERNAL_FAILURES as exc:
        # Unmistakably the outside world: sockets, DNS, timeouts. Contained,
        # but noted as unwrapped so the collector can be tightened later.
        run.status = CollectorStatus.FAILED
        run.error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "%s: external failure not wrapped by the collector — %s", collector.name, exc
        )
    except Exception:
        # Deliberately re-raised. A programming error recorded as FAILED would
        # be indistinguishable from an outage, and the bug would survive.
        run.status = CollectorStatus.FAILED
        run.error = "internal error; see logs"
        run.finished_at = utc_now()
        session.add(run)
        session.commit()
        logger.exception("%s: internal error — re-raising", collector.name)
        raise
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


class CollectorStatusLookup:
    """When a collector last succeeded.

    The input to fundamental freshness. Asking "how old is the filing" would
    answer the wrong question — a quarterly report is old by nature — so the
    question asked instead is "when did we last successfully look".
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def last_success(self, source: object) -> datetime | None:
        """The newest run that got somewhere, complete or not.

        PARTIAL counts because for freshness the question is whether we are
        still reaching the source at all, and a run that fetched most of what
        it wanted plainly was. **That reading is wrong for a collection
        watermark** — see `last_full_success`.
        """
        from sqlalchemy import select

        name = getattr(source, "value", str(source))
        stmt = (
            select(CollectorRun.finished_at)
            .where(
                CollectorRun.source.like(f"{name}%"),
                CollectorRun.status.in_([CollectorStatus.SUCCESS, CollectorStatus.PARTIAL]),
                CollectorRun.finished_at.is_not(None),
            )
            .order_by(CollectorRun.finished_at.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalars().first()

    def last_full_success(self, source: object) -> datetime | None:
        """The newest run that finished everything it set out to do.

        A collector that resumes from where it left off must use this and not
        `last_success`. A run over 2,500 instruments that covered 700 of them
        and ended PARTIAL did not reach the rest, so advancing the watermark to
        its finish time would skip, silently and permanently, the window those
        1,800 instruments were never asked about.

        Taking the earlier timestamp means the next run re-reads a longer
        stretch. That costs calls and loses nothing, which is the direction to
        be wrong in.
        """
        from sqlalchemy import select

        name = getattr(source, "value", str(source))
        stmt = (
            select(CollectorRun.finished_at)
            .where(
                CollectorRun.source.like(f"{name}%"),
                CollectorRun.status == CollectorStatus.SUCCESS,
                CollectorRun.finished_at.is_not(None),
            )
            .order_by(CollectorRun.finished_at.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalars().first()
