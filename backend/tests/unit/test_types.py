"""Factor arithmetic and the guards around it.

The worked example in the design is reproduced here as a test. It is worth
pinning precisely because it demonstrates a trap: four healthy factors can still
produce a total that fails the threshold, purely because one factor sat out
under the ZERO policy. If that arithmetic ever silently changes, the displayed
explanation stops matching the stored numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.types import (
    Availability,
    DataProvenance,
    Engine,
    Factor,
    Freshness,
    Metric,
    MissingFactorPolicy,
    ReasonStatus,
    ScoredSignal,
    SignalAction,
    SignalReason,
)


def provenance(freshness: Freshness = Freshness.FRESH) -> DataProvenance:
    return DataProvenance(
        source_asof=datetime(2026, 9, 19, 6, 30, tzinfo=UTC),
        source_checked_at=datetime(2026, 9, 19, 6, 40, tzinfo=UTC),
        data_age=timedelta(0),
        freshness=freshness,
    )


class TestMetric:
    def test_normalized_must_be_a_percentile(self) -> None:
        with pytest.raises(ValueError, match="0-100"):
            Metric("RSI", raw=61.2, normalized=150.0)

    def test_raw_is_unconstrained(self) -> None:
        """Raw values keep their natural units, including negative ones."""
        assert Metric("MACD", raw=-320.0, normalized=31.0).raw == -320.0


class TestFactor:
    def test_contribution_is_score_times_effective_weight(self) -> None:
        factor = Factor(
            engine=Engine.TECHNICAL,
            score=72.0,
            metrics=(Metric("RSI", 61.2, 68.0),),
            requested_weight=0.40,
            effective_weight=0.40,
            availability=Availability.AVAILABLE,
            provenance=provenance(),
        )
        assert factor.contribution == pytest.approx(28.8)

    def test_contribution_uses_effective_not_requested_weight(self) -> None:
        """The distinction that makes an unusual contribution explainable."""
        factor = Factor(
            engine=Engine.SENTIMENT,
            score=71.0,
            metrics=(),
            requested_weight=0.20,
            effective_weight=0.0,
            availability=Availability.UNAVAILABLE,
            provenance=provenance(Freshness.STALE),
            availability_reason="no data received for 8h (max_age 6h)",
        )
        assert factor.contribution == 0.0

    def test_unavailable_factor_must_carry_a_reason(self) -> None:
        """An unexplained exclusion is indistinguishable from a bug."""
        with pytest.raises(ValueError, match="UNAVAILABLE but carries no reason"):
            Factor(
                engine=Engine.SENTIMENT,
                score=50.0,
                metrics=(),
                requested_weight=0.20,
                effective_weight=0.0,
                availability=Availability.UNAVAILABLE,
                provenance=provenance(Freshness.STALE),
            )

    def test_score_is_bounded(self) -> None:
        with pytest.raises(ValueError, match="score must be 0-100"):
            Factor(
                engine=Engine.TECHNICAL,
                score=120.0,
                metrics=(),
                requested_weight=0.4,
                effective_weight=0.4,
                availability=Availability.AVAILABLE,
                provenance=provenance(),
            )

    def test_weights_are_fractions_not_percentages(self) -> None:
        with pytest.raises(ValueError, match="requested_weight must be 0-1"):
            Factor(
                engine=Engine.TECHNICAL,
                score=72.0,
                metrics=(),
                requested_weight=40.0,
                effective_weight=0.4,
                availability=Availability.AVAILABLE,
                provenance=provenance(),
            )

    def test_factors_are_immutable(self) -> None:
        factor = Factor(
            engine=Engine.TECHNICAL,
            score=72.0,
            metrics=(),
            requested_weight=0.4,
            effective_weight=0.4,
            availability=Availability.AVAILABLE,
            provenance=provenance(),
        )
        with pytest.raises(AttributeError):
            factor.score = 99.0  # type: ignore[misc]


class TestZeroPolicyTrap:
    """The worked example: why ABSTAIN is the default rather than ZERO."""

    @staticmethod
    def build() -> ScoredSignal:
        factors = (
            Factor(
                Engine.TECHNICAL,
                72.0,
                (Metric("RSI", 61.2, 68.0), Metric("MA20 distance", 4.2, 81.0)),
                0.40,
                0.40,
                Availability.AVAILABLE,
                provenance(),
            ),
            Factor(
                Engine.FUNDAMENTAL,
                76.0,
                (Metric("ROE", 14.2, 71.0),),
                0.30,
                0.30,
                Availability.AVAILABLE,
                provenance(),
            ),
            Factor(
                Engine.SENTIMENT,
                71.0,
                (),
                0.20,
                0.0,
                Availability.UNAVAILABLE,
                provenance(Freshness.STALE),
                availability_reason="last post received 8h ago (max_age 6h)",
            ),
            Factor(
                Engine.PORTFOLIO,
                81.0,
                (),
                0.10,
                0.10,
                Availability.AVAILABLE,
                provenance(),
            ),
        )
        total = sum(f.contribution for f in factors)
        return ScoredSignal(
            instrument_id=1,
            data_asof=datetime(2026, 9, 18, 6, 30, tzinfo=UTC),
            decision_at=datetime(2026, 9, 18, 7, 0, tzinfo=UTC),
            earliest_execution_at=datetime(2026, 9, 21, 0, 0, tzinfo=UTC),
            total_score=total,
            action=SignalAction.WATCH,
            factors=factors,
            reasons=(SignalReason(ReasonStatus.SUPPORTS, "MA20 crossed upward", Engine.TECHNICAL),),
            strategy_version="v1.7",
            policy=MissingFactorPolicy.ZERO,
        )

    def test_total_is_the_sum_of_contributions(self) -> None:
        signal = self.build()
        # 28.8 + 22.8 + 0 + 8.1
        assert signal.total_score == pytest.approx(59.7)

    def test_healthy_factors_still_miss_the_threshold(self) -> None:
        """Three factors at 72, 76 and 81 — and the signal still does not fire.

        Under ZERO the scale shrinks with the missing weight while the threshold
        stays at 70, so a single absent factor suppresses signals entirely. The
        fix is not a bigger number; it is ABSTAIN, which declines to judge
        instead of quietly judging on a different scale.
        """
        signal = self.build()
        threshold = 70.0

        assert all(
            f.score > threshold for f in signal.factors if f.availability is Availability.AVAILABLE
        )
        assert signal.total_score < threshold

    def test_effective_weight_total_reveals_the_shrunken_scale(self) -> None:
        signal = self.build()
        assert signal.effective_weight_total == pytest.approx(0.80)
