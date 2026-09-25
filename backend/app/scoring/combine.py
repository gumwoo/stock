"""Combining factors into a signal.

Where the three clocks get set and the execution-timing invariant is enforced.

The rule being protected: a signal computed from a session's close is finalised
*after* that close, so the earliest honest fill is the next session's open.
Filling at the close that produced the decision is look-ahead bias wearing a
different hat — the number was not knowable until the session ended.

`build_signal` raises rather than corrects when that is violated. Silently
adjusting a bad timestamp would let a broken caller keep producing confident,
wrong backtests; the point of an invariant is that breaking it stops the work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.calendar import MarketCalendar
from app.core.clock import ensure_utc
from app.core.types import (
    Availability,
    Engine,
    Factor,
    MissingFactorPolicy,
    ScoredSignal,
    SignalAction,
    SignalReason,
)
from app.scoring.availability import ENGINE_NAME


class ExecutionTimingError(Exception):
    """An internal invariant was violated: a fill could not have happened.

    Not a collector error. External-world problems are isolated and recorded;
    this one propagates, because a system that quietly continues past a
    correctness violation produces numbers that look fine and are not.
    """


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Score boundaries. Part of the strategy version, not constants.

    Boundaries are stated on a full-weight scale and scaled to whatever weight
    actually participated. Under the ZERO policy a missing factor shrinks the
    total without shrinking the thresholds, and comparing the two directly is a
    category error — the score is no longer measured against the same maximum.

    It surfaced immediately and in the worst possible way. Samsung scored 54.6
    on technicals, a thoroughly unremarkable reading, but with no Korean
    fundamentals available its total came to 54.6 x 0.6 = 32.8, which fell
    under a fixed caution threshold of 35. The system was about to tell a user
    to be wary of a stock for no reason other than a gap in our own data.

    Scaling fixes the comparison without renormalizing the weights: technical
    still contributes at 0.6, not silently promoted to 1.0, so the strategy is
    unchanged. Only the yardstick moves.
    """

    buy_interest: float = 70.0
    caution: float = 35.0

    def action_for(self, score: float, effective_weight_total: float = 1.0) -> SignalAction:
        if effective_weight_total <= 0:
            # Nothing participated, so there is nothing to judge.
            return SignalAction.ABSTAINED

        buy = self.buy_interest * effective_weight_total
        caution = self.caution * effective_weight_total

        if score >= buy:
            return SignalAction.BUY_INTEREST
        if score <= caution:
            return SignalAction.CAUTION
        return SignalAction.WATCH


def build_signal(
    *,
    instrument_id: int,
    factors: tuple[Factor, ...],
    reasons: tuple[SignalReason, ...],
    data_asof: datetime,
    decision_at: datetime,
    calendar: MarketCalendar,
    strategy_version: str,
    policy: MissingFactorPolicy = MissingFactorPolicy.ABSTAIN,
    required_factors: frozenset[Engine] = frozenset(),
    thresholds: Thresholds | None = None,
) -> ScoredSignal:
    """Assemble a signal, or abstain from producing one.

    Args:
        data_asof: the data this was computed from, e.g. a session close.
        decision_at: when the judgement was finalised. Must be at or after
            `data_asof` — a decision cannot predate its own inputs.
        calendar: used to derive the earliest honest fill time.
        required_factors: engines whose absence abstains the whole signal
            under ABSTAIN policy.

    Raises:
        ExecutionTimingError: if `decision_at` precedes `data_asof`.
    """
    thresholds = thresholds or Thresholds()
    data_asof = ensure_utc(data_asof, field="data_asof")
    decision_at = ensure_utc(decision_at, field="decision_at")

    if decision_at < data_asof:
        raise ExecutionTimingError(
            f"decision_at {decision_at.isoformat()} precedes data_asof "
            f"{data_asof.isoformat()}: a judgement cannot predate its inputs"
        )

    # The invariant. Derived rather than accepted from the caller, so it cannot
    # be got wrong by a caller that means well.
    earliest_execution_at = calendar.next_tradable_open(decision_at)
    if earliest_execution_at <= decision_at:
        raise ExecutionTimingError(
            f"earliest_execution_at {earliest_execution_at.isoformat()} is not after "
            f"decision_at {decision_at.isoformat()}"
        )

    unavailable_required = {
        f.engine
        for f in factors
        if f.availability is Availability.UNAVAILABLE and f.engine in required_factors
    }

    if unavailable_required and policy is MissingFactorPolicy.ABSTAIN:
        return _abstain(
            instrument_id=instrument_id,
            factors=factors,
            reasons=reasons,
            data_asof=data_asof,
            decision_at=decision_at,
            earliest_execution_at=earliest_execution_at,
            strategy_version=strategy_version,
            missing=unavailable_required,
        )

    total = sum(f.contribution for f in factors)
    weight_total = sum(f.effective_weight for f in factors)

    return ScoredSignal(
        instrument_id=instrument_id,
        data_asof=data_asof,
        decision_at=decision_at,
        earliest_execution_at=earliest_execution_at,
        total_score=total,
        action=thresholds.action_for(total, weight_total),
        factors=factors,
        reasons=reasons,
        strategy_version=strategy_version,
        policy=policy,
    )


def _abstain(
    *,
    instrument_id: int,
    factors: tuple[Factor, ...],
    reasons: tuple[SignalReason, ...],
    data_asof: datetime,
    decision_at: datetime,
    earliest_execution_at: datetime,
    strategy_version: str,
    missing: set[Engine],
) -> ScoredSignal:
    """Decline to judge, without deleting the moment from history.

    The distinction that matters: abstaining means no new entry is suggested
    at this point in time. It does not mean the period is removed from the
    backtest. Deleting periods where data happened to be missing is itself a
    bias, and usually a flattering one.
    """
    names = ", ".join(ENGINE_NAME.get(e, e.value) for e in sorted(missing, key=lambda e: e.value))
    return ScoredSignal(
        instrument_id=instrument_id,
        data_asof=data_asof,
        decision_at=decision_at,
        earliest_execution_at=earliest_execution_at,
        total_score=0.0,
        action=SignalAction.ABSTAINED,
        factors=factors,
        reasons=reasons,
        strategy_version=strategy_version,
        policy=MissingFactorPolicy.ABSTAIN,
        abstained_reason=(
            f"필수 요인을 쓸 수 없음: {names}. "
            "판단하지 않았고, 이미 가진 포지션에는 영향이 없습니다."
        ),
    )
