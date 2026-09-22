"""How many calls we allow ourselves, and over what window.

Pure policy. No database, no clock of its own — the caller passes `now`, which
is what makes every boundary here testable without freezing time globally.

**There are no calendar windows.** Naver's daily cap resets at some hour we
have not confirmed, and a calendar-month window would need month-end and leap
arithmetic besides. Both questions go away under one observation:

    What a daily cap counts is what has been spent since its last reset. Where
    a local day is 24 hours long, that reset is within the last 24 hours, so
    the spent window `[last_reset, now]` sits inside `[now - 24h, now]` and a
    rolling total under budget cannot hide a calendar day over budget.

    (The calendar day as a whole is *not* a subset of `[now - 24h, now]` — part
    of it has not happened yet. What is contained is the part already spent,
    which is also the part the cap counts.)

**That is true of a fixed offset and false on a DST fallback day**, when a
local day runs 25 hours — 26 in `Antarctica/Troll`, 24.5 in
`Australia/Lord_Howe`. On such a day the reset falls an hour or two outside the
rolling window and spending from just after it is not counted. Two things make
that harmless rather than merely unlikely, and both are conditions rather than
luck:

* Every provider metered here resets on a zone without DST. Naver and DART bill
  against Korean time, which has had no summer time since 1988; Threads
  documents its cap as a rolling 24 hours already, which is the same window
  this uses. If a provider on a DST zone is ever added, this argument has to be
  revisited rather than inherited.
* `BUDGET_FRACTION` is a half, so the rolling total would have to double before
  a two-hour uncounted tail could reach a published cap.

A calendar month resets at most 31 days apart under the same condition, so a
rolling 31-day window plays the same role. Every `Quota` below therefore
carries a plain `timedelta`, and month-end and leap years are code that does
not exist.

**The guarantee assumes the clock moves forward at roughly one second per
second.** A host clock that jumps forward by more than a window — a resumed VM,
a large NTP correction — moves the window past every bucket already recorded,
and spending that really happened stops being visible. There is no defence
against that here short of a monotonic clock that survives restarts, which this
does not have. What it does instead: read the time from the database so both
sides of a comparison share one clock, and prune relative to the newest bucket
rather than to the present, so a jump cannot delete live history.

**Windows are floored to the minute, which over-counts on purpose.** The ledger
stores per-minute buckets, so a window whose lower bound falls mid-minute takes
that whole minute in — including calls that have already aged out. That makes
the total an overestimate, never an underestimate, and an overestimate is the
only direction a never-exceed budget is allowed to be wrong in.

**Quotas are grouped, not per endpoint.** Naver meters 25,000 calls a day
against the client ID across its whole search family, not per API. Registering
that limit once per endpoint would let news spend 12,500 and blog spend another
12,500 against the same official cap. So the unit of accounting is
`Quota.group`, and an endpoint is a label for diagnosis only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from app.core.clock import ensure_utc

# How much of each published cap we are willing to spend. Half, because the
# reset timezone is unconfirmed for at least one source and because a budget
# that is routinely near its ceiling tells you nothing when it is finally hit.
BUDGET_FRACTION: float = 0.5


class LimitSource(StrEnum):
    """Where a number in the plan came from.

    Kept on every quota because the plan mixes published caps with ceilings we
    invented, and a table that does not distinguish them is a table whose next
    reader treats our guess as the provider's promise.
    """

    OFFICIAL = "OFFICIAL"
    """Stated in the provider's own documentation."""

    TYPICAL = "TYPICAL"
    """Documented as the usual limit, but explicitly variable by account."""

    INTERNAL = "INTERNAL"
    """A ceiling we chose. The provider has not published this one."""


@dataclass(frozen=True, slots=True)
class Quota:
    """One cap, and the window it is measured over."""

    key: str
    """Stable identifier. Appears in refusal messages and in the CLI."""

    group: str
    """The accounting unit. Calls are summed per group, never per endpoint."""

    official_limit: int
    window: timedelta
    limit_source: LimitSource
    note: str
    """One sentence a human can act on: where the number came from, and what is
    still unconfirmed about it."""

    def __post_init__(self) -> None:
        if self.official_limit <= 0:
            raise ValueError(f"{self.key}: official_limit must be positive")
        if self.window <= timedelta(0):
            raise ValueError(f"{self.key}: window must be positive")


@dataclass(frozen=True, slots=True)
class QuotaPlan:
    """Every quota this build knows about."""

    quotas: tuple[Quota, ...]

    def __post_init__(self) -> None:
        keys = [q.key for q in self.quotas]
        if len(keys) != len(set(keys)):
            raise ValueError("quota keys must be unique")

    def covering(self, group: str) -> tuple[Quota, ...]:
        """Every quota a call against `group` is counted against.

        One call can be covered by several: a Naver news request spends from
        both the rolling-day and the rolling-31-day quota, and both have to
        have room before it is allowed.
        """
        return tuple(q for q in self.quotas if q.group == group)

    def groups(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for quota in self.quotas:
            seen[quota.group] = None
        return tuple(seen)


DEFAULT_PLAN = QuotaPlan(
    (
        Quota(
            key="naver_search_daily",
            group="naver_search",
            official_limit=25_000,
            window=timedelta(hours=24),
            limit_source=LimitSource.OFFICIAL,
            note=(
                "NAVER API Hub meters 25,000 search calls a day against the "
                "application key, shared across news, blog and the rest of the "
                "search family - not a per-endpoint cap. Reset timezone "
                "unconfirmed, which the rolling window makes irrelevant."
            ),
        ),
        Quota(
            key="naver_search_monthly",
            group="naver_search",
            official_limit=775_000,
            window=timedelta(days=31),
            limit_source=LimitSource.OFFICIAL,
            note=(
                "The monthly cap the API Hub console states for this "
                "application, beside the daily one. Both are the product's own "
                "figures rather than ours."
            ),
        ),
        Quota(
            key="naver_datalab_monthly",
            group="naver_datalab",
            official_limit=50_000,
            window=timedelta(days=31),
            limit_source=LimitSource.OFFICIAL,
            note=(
                "API Hub states 50,000 search-trend calls a month and no daily "
                "cap at all. The 1,000-a-day figure belongs to the developer "
                "centre, a different product that no longer issues search "
                "credentials."
            ),
        ),
        Quota(
            key="threads_keyword_search",
            group="threads",
            official_limit=2_200,
            window=timedelta(hours=24),
            limit_source=LimitSource.OFFICIAL,
            note=(
                "2,200 keyword searches per user per rolling 24 hours, counted "
                "across apps rather than per app. Queries returning no results "
                "are documented as not counting; we count them anyway."
            ),
        ),
        Quota(
            key="reddit_search",
            group="reddit",
            official_limit=1_000,
            window=timedelta(minutes=10),
            limit_source=LimitSource.OFFICIAL,
            note="100 queries per minute per OAuth client, averaged over ten minutes.",
        ),
        Quota(
            key="dart_daily",
            group="dart",
            official_limit=20_000,
            window=timedelta(hours=24),
            limit_source=LimitSource.TYPICAL,
            note=(
                "DART documents that requests past roughly 20,000 in a day draw "
                "a limit error, while saying the threshold varies by account "
                "configuration. Treated as typical rather than promised."
            ),
        ),
    )
)


def budget(quota: Quota, *, fraction: float = BUDGET_FRACTION) -> int:
    """How many calls we allow ourselves against `quota`.

    Floored, not rounded: rounding half a call up would spend a call we said we
    would not. Done in `Decimal`, because binary floating point can land a
    hair above the exact product and hand the floor an extra call — 4,072,150
    at 0.3 comes out one too high in `float`.
    """
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"fraction must be in (0, 1], got {fraction}")
    return int(
        (Decimal(quota.official_limit) * Decimal(str(fraction))).to_integral_value(ROUND_FLOOR)
    )


def window_start(quota: Quota, now: datetime) -> datetime:
    """The inclusive lower bound of the window, floored to the minute.

    Flooring pulls the bound backwards, so the boundary minute's bucket is
    counted whole. Some of those calls have already aged out of the true
    window, which makes the total an overestimate — the only direction a
    never-exceed budget may be wrong in.
    """
    return (ensure_utc(now, field="now") - quota.window).replace(second=0, microsecond=0)


def headroom(quota: Quota, spent: int, *, fraction: float = BUDGET_FRACTION) -> int:
    """Calls still available against `quota`. Never negative."""
    return max(0, budget(quota, fraction=fraction) - spent)


def floor_to_minute(moment: datetime) -> datetime:
    """The bucket a call at `moment` belongs to."""
    return ensure_utc(moment, field="moment").replace(second=0, microsecond=0)


def describe(quotas: Iterable[Quota], *, fraction: float = BUDGET_FRACTION) -> str:
    """One line per quota, for the CLI and for refusal messages."""
    return "\n".join(
        f"{q.key:24s} {budget(q, fraction=fraction):>8,d} of {q.official_limit:>8,d} "
        f"per {q.window} [{q.limit_source}]"
        for q in quotas
    )
