"""The gate every outbound call passes through.

`TokenBucket` next door shapes the rate within a second. This bounds the total
over a day or a month, which is the limit that actually gets exceeded: a bucket
refills forever, so a loop that keeps asking keeps being served, and a daily cap
can go in minutes. DART only learns it went over when the server says so, which
is finding out afterwards. Here the budget is checked before the call, so going
over is not a thing that can happen and then be reported.

Two rules below break the house convention that repositories never commit and
the collector commits once at the end. Both come straight out of "never exceed".

**The call is recorded before it is made, not after.** Providers count requests,
not successes. Recording afterwards loses the count whenever the process dies
mid-request, and a lost count is an undercount, which is how a budget gets
exceeded. Recording first means the worst case is counting a call we never made:
an overcount, which only costs us headroom.

**The reservation commits in its own transaction, separate from the collector's.**
If a run fails on its third page and its session rolls back, the two pages that
genuinely went out would vanish from the ledger along with the data. That
rollback is right about the data and wrong about the accounting; the two axes
cannot share a transaction.

Anyone tidying this up later should read those two paragraphs first. A rule
broken without its reason beside it gets "fixed" by the next reader.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.collectors.base import SkipCollection
from app.config import get_settings
from app.core.quota import (
    BUDGET_FRACTION,
    DEFAULT_PLAN,
    Quota,
    QuotaPlan,
    budget,
    floor_to_minute,
    headroom,
    window_start,
)
from app.db import session_scope
from app.repositories import quota_repo

# Buckets older than the longest window answer no question. Kept a little past
# it so that a window extended slightly does not read into a pruned gap.
RETENTION = timedelta(days=40)


def plan_from_settings() -> QuotaPlan:
    """`DEFAULT_PLAN` with the published caps read from configuration.

    The numbers and the reasoning live in `app.core.quota`; this only lets them
    be overridden. That exists for one practical reason: proving the refusal
    path works should cost two calls, not twelve thousand. Set
    `NAVER_SEARCH_DAILY_LIMIT=4` and the budget becomes 2.

    Anything not named here keeps the plan's own figure.
    """
    settings = get_settings()
    overrides = {
        "naver_search_daily": settings.naver_search_daily_limit,
        "naver_search_monthly": settings.naver_search_internal_31d_limit,
        "naver_datalab_monthly": settings.naver_datalab_monthly_limit,
        "dart_daily": settings.dart_daily_limit,
    }
    return QuotaPlan(
        tuple(
            replace(quota, official_limit=overrides[quota.key]) if quota.key in overrides else quota
            for quota in DEFAULT_PLAN.quotas
        )
    )


class QuotaExhausted(SkipCollection):
    """We declined to make a call because our own budget had no room.

    A subclass of `SkipCollection` on purpose. This is not an outage and not a
    failure: the machinery worked, and what it decided was to stop. Running out
    of our own budget belongs in the same box as having no credentials —
    nothing is broken, and the detail says what would change it.

    Distinct from `RateLimitedError`, which means the *provider* turned us
    away. That one is a failure, loudly, because it means the ledger and the
    provider disagree and our accounting is wrong.

    **A collector that has already done work must catch this rather than let
    it escape.** Uncaught, `run_collector` records the run as SKIPPED with
    `items_read=0`, which would be a lie about a sweep that collected half a
    market before the budget ran out. Letting it escape is right only when
    nothing has been collected yet; after that the honest status is PARTIAL,
    and the collector is the only place that knows which of the two it is.
    """

    def __init__(
        self,
        *,
        quota: Quota,
        spent: int,
        allowed: int,
        retry_after: datetime | None,
    ) -> None:
        when = (
            f", headroom returns around {retry_after:%Y-%m-%d %H:%M} UTC"
            if retry_after is not None
            else ""
        )
        super().__init__(
            f"{quota.key}: {spent} of {allowed} calls used in the last {quota.window} "
            f"(our budget, {BUDGET_FRACTION:.0%} of the {quota.limit_source.lower()} "
            f"limit of {quota.official_limit:,}){when}"
        )
        self.quota = quota
        self.spent = spent
        self.allowed = allowed
        self.retry_after = retry_after


class QuotaGuard:
    """Reserves calls against the ledger, or refuses them."""

    __slots__ = ("_fraction", "_plan", "_scope")

    def __init__(
        self,
        *,
        plan: QuotaPlan | None = None,
        fraction: float | None = None,
        scope: Callable[[], AbstractContextManager[Session]] = session_scope,
    ) -> None:
        self._plan = plan if plan is not None else plan_from_settings()
        self._fraction = fraction if fraction is not None else get_settings().quota_budget_fraction
        # Injectable so a test can hand over a session of its own. Production
        # always wants `session_scope`, which is what makes the reservation its
        # own transaction rather than part of the collector's.
        self._scope = scope

    @property
    def plan(self) -> QuotaPlan:
        return self._plan

    def reserve(
        self, group: str, endpoint: str, *, calls: int = 1, now: datetime | None = None
    ) -> None:
        """Take `calls` from every quota covering `group`, or refuse them all.

        Raises `QuotaExhausted` without writing anything if any covering quota
        lacks room. All or nothing: a call that half-reserves would be counted
        against one cap and not another.
        """
        if calls <= 0:
            raise ValueError(f"calls must be positive, got {calls}")

        quotas = self._plan.covering(group)
        if not quotas:
            raise ValueError(f"no quota registered for group {group!r}")

        with self._scope() as session:
            # Held to the end of this transaction, so the check and the write
            # cannot be interleaved with another process doing the same.
            quota_repo.lock_group(session, group)

            # After the lock, not before. Waiting on another reservation can
            # take seconds, and a moment captured first would file the call
            # under a minute that had already passed - an undercount by however
            # long the wait lasted.
            moment = now if now is not None else quota_repo.db_now(session)

            for quota in quotas:
                since = window_start(quota, moment)
                spent = quota_repo.spent_since(session, group=group, since=since)
                allowed = budget(quota, fraction=self._fraction)
                if spent + calls > allowed:
                    oldest = quota_repo.oldest_minute_since(session, group=group, since=since)
                    raise QuotaExhausted(
                        quota=quota,
                        spent=spent,
                        allowed=allowed,
                        retry_after=oldest + quota.window if oldest else None,
                    )

            quota_repo.record_calls(
                session,
                group=group,
                endpoint=endpoint,
                minute_start=floor_to_minute(moment),
                calls=calls,
            )

    def remaining(self, group: str, *, now: datetime | None = None) -> Mapping[str, int]:
        """Calls left against each quota covering `group`. Costs no quota."""
        with self._scope() as session:
            moment = now if now is not None else quota_repo.db_now(session)
            return {
                quota.key: headroom(
                    quota,
                    quota_repo.spent_since(session, group=group, since=window_start(quota, moment)),
                    fraction=self._fraction,
                )
                for quota in self._plan.covering(group)
            }

    def report(self, *, now: datetime | None = None) -> list[tuple[Quota, int, int]]:
        """Every quota with its spend and its budget, for the CLI."""
        with self._scope() as session:
            moment = now if now is not None else quota_repo.db_now(session)
            return [
                (
                    quota,
                    quota_repo.spent_since(
                        session, group=quota.group, since=window_start(quota, moment)
                    ),
                    budget(quota, fraction=self._fraction),
                )
                for quota in self._plan.quotas
            ]

    def prune(self, *, keep: timedelta = RETENTION, now: datetime | None = None) -> int:
        """Drop buckets no window can still reach.

        Measured back from the newest bucket rather than from the present. A
        clock that jumped forward would otherwise make every real bucket look
        older than retention and delete spending all six windows still count.
        """
        with self._scope() as session:
            newest = quota_repo.newest_minute(session)
            if newest is None:
                return 0
            return quota_repo.prune_before(session, cutoff=newest - keep)
