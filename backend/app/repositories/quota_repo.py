"""Reading and writing the API call ledger.

Windows are answered by summing per-minute buckets, always over `quota_group`
and never over `endpoint` — the reasoning is on `app.models.quota`.

Like every repository here, nothing in this module commits. The one caller that
must commit on its own is `QuotaGuard`, and it does so in a transaction of its
own for reasons written down beside it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.db import _lock_key
from app.models.quota import ApiCallBucket


def db_now(session: Session) -> datetime:
    """The database's clock, not the process's.

    One clock for both sides of every comparison. The two would otherwise be
    allowed to disagree, and a ledger whose reads and writes are timed by
    different clocks has a drift budget nobody set.
    """
    moment = session.execute(text("SELECT now()")).scalar_one()
    return ensure_utc(moment, field="db_now")


def lock_group(session: Session, group: str) -> None:
    """Hold a transaction-scoped advisory lock on `group`.

    Makes check-then-increment atomic. A single worker still needs it: the user
    can run `python -m app.cli collect` by hand while the scheduler is midway
    through the same source, and that window is real even if it is small.

    Transaction-scoped rather than session-scoped, so the lock is released by
    the commit or rollback that ends the reservation. There is nothing to
    unlock by hand and no path where an error leaves it held.
    """
    # Fail loudly rather than hang. A caller holding an uncommitted row in the
    # bucket this reservation is about to touch would block it forever, and
    # Postgres cannot call that a deadlock because there is no cycle - the
    # caller is stuck inside `reserve` and so can never release what it holds.
    # Unreachable today, since nothing but the guard writes to this table, but
    # a silent hang is the worst way to discover that has changed.
    session.execute(text("SET LOCAL lock_timeout = '10s'"))
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(f"quota:{group}")})


def spent_since(session: Session, *, group: str, since: datetime) -> int:
    """Calls recorded against `group` in buckets at or after `since`.

    `since` is expected to be floored to the minute by `core.quota.window_start`,
    which makes this an overestimate of the true window. That is the intended
    direction: a budget that is never exceeded may only ever over-count.

    Bounded below only. A bucket dated after `since` always counts, including
    one dated after the moment being asked about — which no real run produces,
    since time only goes forward, but clock skew could. Counting it overstates
    the spend; excluding it would understate, and understating is how a cap
    gets exceeded.
    """
    stmt = select(func.coalesce(func.sum(ApiCallBucket.calls), 0)).where(
        ApiCallBucket.quota_group == group,
        ApiCallBucket.minute_start >= ensure_utc(since, field="since"),
    )
    return int(session.execute(stmt).scalar_one())


def record_calls(
    session: Session, *, group: str, endpoint: str, minute_start: datetime, calls: int
) -> None:
    """Add `calls` to the bucket for this group, endpoint and minute."""
    if calls <= 0:
        raise ValueError(f"calls must be positive, got {calls}")

    stmt = pg_insert(ApiCallBucket).values(
        quota_group=group,
        endpoint=endpoint,
        minute_start=ensure_utc(minute_start, field="minute_start"),
        calls=calls,
    )
    session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_api_call_bucket_slot",
            set_={"calls": ApiCallBucket.calls + stmt.excluded.calls},
        )
    )


def oldest_minute_since(session: Session, *, group: str, since: datetime) -> datetime | None:
    """The earliest bucket still inside the window, or None if it is empty.

    Only used on the refusal path, to say when headroom comes back: that bucket
    leaves the window one window-length after the minute it covers. Without it
    the only honest answer is "within a day", which tells nobody anything.
    """
    stmt = select(func.min(ApiCallBucket.minute_start)).where(
        ApiCallBucket.quota_group == group,
        ApiCallBucket.minute_start >= ensure_utc(since, field="since"),
    )
    return session.execute(stmt).scalar()


def usage_by_endpoint(session: Session, *, group: str, since: datetime) -> dict[str, int]:
    """Who spent the group's budget, for diagnosis. Never a window predicate."""
    stmt = (
        select(ApiCallBucket.endpoint, func.coalesce(func.sum(ApiCallBucket.calls), 0))
        .where(
            ApiCallBucket.quota_group == group,
            ApiCallBucket.minute_start >= ensure_utc(since, field="since"),
        )
        .group_by(ApiCallBucket.endpoint)
        .order_by(ApiCallBucket.endpoint)
    )
    return {endpoint: int(total) for endpoint, total in session.execute(stmt)}


def newest_minute(session: Session, *, group: str | None = None) -> datetime | None:
    """The latest bucket on record. The ledger's own idea of "recently"."""
    stmt = select(func.max(ApiCallBucket.minute_start))
    if group is not None:
        stmt = stmt.where(ApiCallBucket.quota_group == group)
    return session.execute(stmt).scalar()


def prune_before(session: Session, *, cutoff: datetime) -> int:
    """Drop buckets older than `cutoff`. Returns rows removed.

    Nothing reads past the longest window, so keeping more is storage spent on
    a question no code asks. The caller leaves margin above that window, and
    derives `cutoff` from the newest bucket rather than from the present - a
    clock that jumped forward would otherwise delete history still inside
    every window.
    """
    result = session.execute(
        delete(ApiCallBucket).where(ApiCallBucket.minute_start < ensure_utc(cutoff, field="cutoff"))
    )
    return int(getattr(result, "rowcount", 0) or 0)
