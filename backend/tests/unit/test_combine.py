"""Signal assembly, execution timing and abstention.

The execution-timing tests are the counterpart to the point-in-time tests. PIT
stops the system reading data it could not have had; this stops it *trading* at
a price it could not have got. Both are look-ahead bias, and blocking only one
of them still produces a backtest that beats reality.

The headline case is `test_close_decision_cannot_fill_on_that_close`: a signal
computed from Friday's close fills on Monday's open, never on Friday.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.calendar import Market, MarketCalendar
from app.core.types import (
    Availability,
    DataProvenance,
    Engine,
    Factor,
    Freshness,
    Metric,
    MissingFactorPolicy,
    SignalAction,
)
from app.scoring.combine import ExecutionTimingError, Thresholds, build_signal

KR = MarketCalendar(Market.KR)
FRESH = DataProvenance(None, None, timedelta(0), Freshness.FRESH)
STALE = DataProvenance(None, None, timedelta(hours=9), Freshness.STALE)


def factor(
    engine: Engine,
    score: float,
    weight: float,
    *,
    available: bool = True,
) -> Factor:
    return Factor(
        engine=engine,
        score=score,
        metrics=(Metric("m", raw=score, normalized=score),),
        requested_weight=weight,
        effective_weight=weight if available else 0.0,
        availability=Availability.AVAILABLE if available else Availability.UNAVAILABLE,
        provenance=FRESH if available else STALE,
        availability_reason=None if available else "no data for 9h",
    )


class TestExecutionTiming:
    def test_close_decision_cannot_fill_on_that_close(self) -> None:
        """The core rule.

        Friday's close is not knowable until Friday ends, so a decision based
        on it cannot have been executed at it. The earliest honest fill is
        Monday's open.
        """
        friday_close = KR.session_close(datetime(2026, 9, 18, tzinfo=UTC).date())

        signal = build_signal(
            instrument_id=1,
            factors=(factor(Engine.TECHNICAL, 80.0, 1.0),),
            reasons=(),
            data_asof=friday_close,
            decision_at=friday_close,
            calendar=KR,
            strategy_version="v1",
        )

        assert signal.earliest_execution_at == datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
        assert signal.earliest_execution_at > friday_close
        assert signal.earliest_execution_at > signal.data_asof

    def test_earliest_execution_is_always_after_the_decision(self) -> None:
        for hour in (1, 6, 12, 23):
            moment = datetime(2026, 9, 16, hour, 0, tzinfo=UTC)
            signal = build_signal(
                instrument_id=1,
                factors=(factor(Engine.TECHNICAL, 50.0, 1.0),),
                reasons=(),
                data_asof=moment,
                decision_at=moment,
                calendar=KR,
                strategy_version="v1",
            )
            assert signal.earliest_execution_at > signal.decision_at

    def test_holiday_pushes_the_fill_further_out(self) -> None:
        """A decision before a market holiday waits for the market to reopen."""
        us = MarketCalendar(Market.US)
        before_july_fourth = datetime(2026, 7, 2, 21, 0, tzinfo=UTC)

        signal = build_signal(
            instrument_id=1,
            factors=(factor(Engine.TECHNICAL, 50.0, 1.0),),
            reasons=(),
            data_asof=before_july_fourth,
            decision_at=before_july_fourth,
            calendar=us,
            strategy_version="v1",
        )

        assert signal.earliest_execution_at.date() == datetime(2026, 7, 6, tzinfo=UTC).date()

    def test_decision_before_its_own_data_is_rejected(self) -> None:
        """Fail fast rather than repair. A caller that got this wrong is broken."""
        with pytest.raises(ExecutionTimingError, match="cannot predate its inputs"):
            build_signal(
                instrument_id=1,
                factors=(factor(Engine.TECHNICAL, 50.0, 1.0),),
                reasons=(),
                data_asof=datetime(2026, 9, 18, 6, 30, tzinfo=UTC),
                decision_at=datetime(2026, 9, 18, 5, 0, tzinfo=UTC),
                calendar=KR,
                strategy_version="v1",
            )

    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            build_signal(
                instrument_id=1,
                factors=(factor(Engine.TECHNICAL, 50.0, 1.0),),
                reasons=(),
                data_asof=datetime(2026, 9, 18, 6, 30),
                decision_at=datetime(2026, 9, 18, 7, 0, tzinfo=UTC),
                calendar=KR,
                strategy_version="v1",
            )


class TestScoring:
    MOMENT = datetime(2026, 9, 18, 6, 30, tzinfo=UTC)

    def test_total_is_the_sum_of_contributions(self) -> None:
        signal = build_signal(
            instrument_id=1,
            factors=(
                factor(Engine.TECHNICAL, 72.0, 0.40),
                factor(Engine.FUNDAMENTAL, 76.0, 0.30),
                factor(Engine.PORTFOLIO, 81.0, 0.10),
            ),
            reasons=(),
            data_asof=self.MOMENT,
            decision_at=self.MOMENT,
            calendar=KR,
            strategy_version="v1",
        )
        assert signal.total_score == pytest.approx(59.7)

    def test_thresholds_map_score_to_action(self) -> None:
        thresholds = Thresholds(buy_interest=70.0, caution=35.0)
        assert thresholds.action_for(85.0) is SignalAction.BUY_INTEREST
        assert thresholds.action_for(50.0) is SignalAction.WATCH
        assert thresholds.action_for(20.0) is SignalAction.CAUTION


class TestAbstention:
    MOMENT = datetime(2026, 9, 18, 6, 30, tzinfo=UTC)

    def build(self, policy: MissingFactorPolicy):  # type: ignore[no-untyped-def]
        return build_signal(
            instrument_id=1,
            factors=(
                factor(Engine.TECHNICAL, 72.0, 0.40),
                factor(Engine.FUNDAMENTAL, 76.0, 0.30, available=False),
                factor(Engine.PORTFOLIO, 81.0, 0.10),
            ),
            reasons=(),
            data_asof=self.MOMENT,
            decision_at=self.MOMENT,
            calendar=KR,
            strategy_version="v1",
            policy=policy,
            required_factors=frozenset({Engine.TECHNICAL, Engine.FUNDAMENTAL}),
        )

    def test_missing_required_factor_abstains(self) -> None:
        signal = self.build(MissingFactorPolicy.ABSTAIN)

        assert signal.action is SignalAction.ABSTAINED
        assert signal.abstained_reason is not None
        assert "FUNDAMENTAL" in signal.abstained_reason

    def test_abstention_says_existing_positions_are_untouched(self) -> None:
        """Declining to judge is not an instruction to do anything."""
        signal = self.build(MissingFactorPolicy.ABSTAIN)

        assert signal.abstained_reason is not None
        assert "existing positions are unaffected" in signal.abstained_reason

    def test_abstention_still_records_the_moment(self) -> None:
        """The period is not deleted — removing it would itself be a bias.

        A backtest that drops every date where data was missing reports the
        performance of a strategy that somehow knew when to sit out.
        """
        signal = self.build(MissingFactorPolicy.ABSTAIN)

        assert signal.data_asof == self.MOMENT
        assert signal.earliest_execution_at > signal.decision_at
        assert len(signal.factors) == 3  # every factor retained, including the absent one

    def test_zero_policy_scores_on_a_shrunken_scale(self) -> None:
        """The trap ABSTAIN exists to avoid.

        Under ZERO the missing weight is simply lost, so the total is measured
        against a smaller maximum while the threshold stays where it was.
        """
        signal = self.build(MissingFactorPolicy.ZERO)

        assert signal.action is not SignalAction.ABSTAINED
        assert signal.total_score == pytest.approx(36.9)  # 28.8 + 0 + 8.1
        assert signal.effective_weight_total == pytest.approx(0.50)

    def test_unavailable_non_required_factor_does_not_abstain(self) -> None:
        signal = build_signal(
            instrument_id=1,
            factors=(
                factor(Engine.TECHNICAL, 72.0, 0.40),
                factor(Engine.SENTIMENT, 71.0, 0.20, available=False),
            ),
            reasons=(),
            data_asof=self.MOMENT,
            decision_at=self.MOMENT,
            calendar=KR,
            strategy_version="v1",
            policy=MissingFactorPolicy.ABSTAIN,
            required_factors=frozenset({Engine.TECHNICAL}),
        )

        assert signal.action is not SignalAction.ABSTAINED
        assert signal.total_score == pytest.approx(28.8)
