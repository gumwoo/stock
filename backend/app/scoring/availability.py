"""Freshness and factor availability.

A collector failing and a factor being unusable are different events, and
collapsing them is a real bug rather than a simplification. If the DART
collector failed this morning, the quarterly filing we collected last week is
still perfectly good; excluding the fundamental factor because of it would
change the signal for no reason.

So the decision runs through a chain, and each link asks a different question:

    Collector Health  ->  Data Freshness  ->  Factor Availability  ->  Policy
    (did the run work)   (is the data ok)    (may we use it)        (what now)

Freshness itself is judged **differently per factor**, because "old" means
different things:

    technical     against trading sessions. A wall-clock rule of one day marks
                  Friday's close stale on Monday morning, which is nonsense —
                  the market was shut.

    news/social   against wall-clock age. This data genuinely should keep
                  flowing; eight hours of silence means something is wrong.

    fundamental   against when the source was last successfully checked, not
                  against the age of the filing. A quarterly report is old by
                  nature. What matters is whether we are still looking, because
                  the risk is missing a *new* filing, not holding an old one.

This module is pure: it takes facts and returns a verdict. It performs no IO,
which is what lets the whole matrix be tested against frozen clocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.core.calendar import MarketCalendar
from app.core.clock import ensure_utc
from app.core.types import (
    Availability,
    DataProvenance,
    Engine,
    Freshness,
    MissingFactorPolicy,
)


@dataclass(frozen=True, slots=True)
class SessionFreshnessRule:
    """Data must be no older than the most recently completed session.

    Used for market data, where calendar days and trading days diverge over
    weekends and holidays.
    """

    max_sessions_behind: int = 1


@dataclass(frozen=True, slots=True)
class WallClockFreshnessRule:
    """Data must have arrived within a fixed duration.

    Used for news and social, which should be flowing continuously.
    """

    max_age: timedelta


@dataclass(frozen=True, slots=True)
class SourceCheckFreshnessRule:
    """The *source* must have been checked recently; the data may be old.

    Used for fundamentals. The filing's age is irrelevant — a Q2 report in
    September is normal. The question is whether we would notice a new one.
    """

    max_check_age: timedelta


FreshnessRule = SessionFreshnessRule | WallClockFreshnessRule | SourceCheckFreshnessRule


def evaluate_freshness(
    rule: FreshnessRule,
    *,
    now: datetime,
    source_asof: datetime | None,
    source_checked_at: datetime | None = None,
    calendar: MarketCalendar | None = None,
) -> DataProvenance:
    """Judge how current a factor's inputs are, using the rule for its kind.

    Args:
        rule: which of the three kinds of staleness applies.
        now: evaluation instant (timezone-aware).
        source_asof: timestamp of the newest underlying datum, if any.
        source_checked_at: when the source was last successfully reached.
            Required by `SourceCheckFreshnessRule`; ignored by the others.
        calendar: required by `SessionFreshnessRule` to count trading sessions.

    Returns:
        Provenance carrying the verdict plus the numbers behind it, so the UI
        can show *why* something was excluded rather than only that it was.
    """
    now = ensure_utc(now, field="now")
    age = now - ensure_utc(source_asof, field="source_asof") if source_asof else None

    match rule:
        case SourceCheckFreshnessRule():
            freshness = _judge_source_check(rule, now, source_asof, source_checked_at)
        case WallClockFreshnessRule():
            freshness = _judge_wall_clock(rule, age, source_asof)
        case SessionFreshnessRule():
            freshness = _judge_sessions(rule, now, source_asof, calendar)

    return DataProvenance(
        source_asof=source_asof,
        source_checked_at=source_checked_at,
        data_age=age,
        freshness=freshness,
    )


def _judge_source_check(
    rule: SourceCheckFreshnessRule,
    now: datetime,
    source_asof: datetime | None,
    source_checked_at: datetime | None,
) -> Freshness:
    """Fundamentals: old data is fine, an unwatched source is not."""
    if source_asof is None:
        return Freshness.MISSING
    if source_checked_at is None:
        return Freshness.STALE
    checked_age = now - ensure_utc(source_checked_at, field="source_checked_at")
    return Freshness.FRESH if checked_age <= rule.max_check_age else Freshness.STALE


def _judge_wall_clock(
    rule: WallClockFreshnessRule,
    age: timedelta | None,
    source_asof: datetime | None,
) -> Freshness:
    """News and social: silence is the signal that something broke."""
    if source_asof is None or age is None:
        return Freshness.MISSING
    return Freshness.FRESH if age <= rule.max_age else Freshness.STALE


def _judge_sessions(
    rule: SessionFreshnessRule,
    now: datetime,
    source_asof: datetime | None,
    calendar: MarketCalendar | None,
) -> Freshness:
    """Market data: count trading sessions, not calendar days."""
    if source_asof is None:
        return Freshness.MISSING
    if calendar is None:
        raise ValueError("SessionFreshnessRule requires a calendar")

    data_day = ensure_utc(source_asof, field="source_asof").date()
    sessions = _sessions_between(calendar, data_day, now.date())
    return Freshness.FRESH if sessions <= rule.max_sessions_behind else Freshness.STALE


def _sessions_between(calendar: MarketCalendar, start: date, end: date) -> int:
    """Count trading sessions strictly after `start` up to and including `end`.

    Friday data looked at on Monday gives 1, not 3, because Saturday and Sunday
    are not sessions. That is the whole reason this is not a subtraction.
    """
    if end <= start:
        return 0
    count = 0
    cursor = start
    while cursor < end:
        cursor = calendar.next_session(cursor)
        if cursor > end:
            break
        count += 1
    return count


@dataclass(frozen=True, slots=True)
class AvailabilityVerdict:
    """Whether a factor may be scored, and the weight it actually gets."""

    availability: Availability
    effective_weight: float
    reason: str | None = None


def resolve_availability(
    engine: Engine,
    *,
    provenance: DataProvenance,
    requested_weight: float,
    policy: MissingFactorPolicy,
    is_required: bool,
    surviving_weight_total: float = 1.0,
) -> AvailabilityVerdict:
    """Turn a freshness verdict into an effective weight.

    Args:
        engine: which factor this is, for the explanatory message.
        provenance: the freshness verdict from `evaluate_freshness`.
        requested_weight: what the strategy config asked for.
        policy: what to do when a factor is unusable.
        is_required: whether its absence should abstain the whole signal.
        surviving_weight_total: sum of requested weights of the factors that
            remain, used only by RENORMALIZE.

    Note that `ABSTAIN` is not handled by zeroing a weight — the caller detects
    a required factor coming back UNAVAILABLE and declines to emit a signal at
    all. Declining to judge is different from judging on a reduced scale.
    """
    if provenance.freshness is Freshness.FRESH:
        return AvailabilityVerdict(Availability.AVAILABLE, requested_weight)

    reason = _explain(engine, provenance)

    if is_required and policy is MissingFactorPolicy.ABSTAIN:
        return AvailabilityVerdict(Availability.UNAVAILABLE, 0.0, reason)

    match policy:
        case MissingFactorPolicy.ABSTAIN | MissingFactorPolicy.ZERO:
            # Weight drops to zero and is *not* redistributed, so the total's
            # scale shrinks. Callers must scale thresholds accordingly, which
            # is precisely why ABSTAIN is the recommended default.
            return AvailabilityVerdict(Availability.UNAVAILABLE, 0.0, reason)
        case MissingFactorPolicy.RENORMALIZE:
            return AvailabilityVerdict(Availability.UNAVAILABLE, 0.0, reason)


def renormalized_weights(
    requested: dict[Engine, float],
    unavailable: set[Engine],
) -> dict[Engine, float]:
    """Redistribute weight across the surviving factors.

    Opt-in only. Redistributing turns `40/30/20/10` into `50/37.5/0/12.5`,
    which is a different strategy than the one that was configured — so runs
    using it must record that they did, and comparisons across periods where
    availability differed are not like-for-like.
    """
    survivors = {e: w for e, w in requested.items() if e not in unavailable}
    total = sum(survivors.values())
    if total <= 0:
        return dict.fromkeys(requested, 0.0)
    scaled = {e: w / total for e, w in survivors.items()}
    return {e: scaled.get(e, 0.0) for e in requested}


def _explain(engine: Engine, provenance: DataProvenance) -> str:
    """A reason a person can act on, not just a status code."""
    if provenance.freshness is Freshness.MISSING:
        return f"{engine.value.lower()}: no data available"

    if provenance.source_checked_at is not None and provenance.source_asof is not None:
        return (
            f"{engine.value.lower()}: source last checked "
            f"{provenance.source_checked_at:%Y-%m-%d %H:%M} UTC — too long ago, "
            "a newer filing may have been missed"
        )

    if provenance.data_age is not None:
        hours = provenance.data_age.total_seconds() / 3600
        return f"{engine.value.lower()}: newest data is {hours:.1f}h old"

    return f"{engine.value.lower()}: stale"
