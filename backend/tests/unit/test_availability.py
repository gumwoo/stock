"""Freshness and availability.

These tests exist because of one specific bug that is easy to write and hard to
notice: treating a collector failure as though the factor's data had vanished.

The headline case is `test_old_filing_with_recently_checked_source_is_fresh` —
a filing 36 days old, a source checked 2 hours ago, verdict FRESH. Judging
fundamentals by a max_age would mark that STALE and silently drop the factor,
changing signals for no reason at all.

The weekend case matters for the same reason in the other direction: a
wall-clock rule marks Friday's close stale on Monday morning, when in fact no
trading happened in between.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.calendar import Market, MarketCalendar
from app.core.types import Availability, Engine, Freshness, MissingFactorPolicy
from app.scoring.availability import (
    SessionFreshnessRule,
    SourceCheckFreshnessRule,
    WallClockFreshnessRule,
    evaluate_freshness,
    renormalized_weights,
    resolve_availability,
)

KR = MarketCalendar(Market.KR)


class TestFundamentalFreshness:
    """Judged by source-check recency, never by the age of the filing."""

    RULE = SourceCheckFreshnessRule(max_check_age=timedelta(days=3))

    def test_old_filing_with_recently_checked_source_is_fresh(self) -> None:
        """The case the whole design turns on.

        A quarterly filing 36 days old is not stale data — it is simply what a
        quarterly filing looks like between reports. What matters is that we
        checked SEC/DART recently enough to have caught a newer one.
        """
        now = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)

        provenance = evaluate_freshness(
            self.RULE,
            now=now,
            source_asof=now - timedelta(days=36),
            source_checked_at=now - timedelta(hours=2),
        )

        assert provenance.freshness is Freshness.FRESH
        assert provenance.data_age == timedelta(days=36)

    def test_same_filing_with_neglected_source_is_stale(self) -> None:
        """Identical data, different verdict — because we stopped looking."""
        now = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)

        provenance = evaluate_freshness(
            self.RULE,
            now=now,
            source_asof=now - timedelta(days=36),
            source_checked_at=now - timedelta(days=10),
        )

        assert provenance.freshness is Freshness.STALE
        assert provenance.data_age == timedelta(days=36)

    def test_data_age_does_not_decide_the_verdict(self) -> None:
        """Explicitly: a 200-day-old filing is still fresh if we are watching."""
        now = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)

        provenance = evaluate_freshness(
            self.RULE,
            now=now,
            source_asof=now - timedelta(days=200),
            source_checked_at=now - timedelta(hours=1),
        )

        assert provenance.freshness is Freshness.FRESH

    def test_never_checked_is_stale(self) -> None:
        now = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)
        provenance = evaluate_freshness(
            self.RULE, now=now, source_asof=now - timedelta(days=1), source_checked_at=None
        )
        assert provenance.freshness is Freshness.STALE

    def test_no_data_at_all_is_missing_not_stale(self) -> None:
        """MISSING and STALE are different: one never arrived, one aged out."""
        now = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)
        provenance = evaluate_freshness(self.RULE, now=now, source_asof=None, source_checked_at=now)
        assert provenance.freshness is Freshness.MISSING


class TestTechnicalFreshness:
    """Judged in trading sessions, because weekends are not staleness."""

    RULE = SessionFreshnessRule(max_sessions_behind=1)

    def test_friday_close_is_fresh_on_monday_morning(self) -> None:
        """The weekend case.

        A wall-clock rule of one day would call this stale after 72 hours. But
        no session happened in between, so the data is the latest that exists.
        """
        friday_close = datetime(2026, 9, 18, 6, 30, tzinfo=UTC)
        monday_morning = datetime(2026, 9, 21, 0, 30, tzinfo=UTC)

        provenance = evaluate_freshness(
            self.RULE, now=monday_morning, source_asof=friday_close, calendar=KR
        )

        assert provenance.freshness is Freshness.FRESH
        assert provenance.data_age is not None
        assert provenance.data_age > timedelta(days=2)  # old by the clock, current by the market

    def test_same_day_data_is_fresh(self) -> None:
        provenance = evaluate_freshness(
            self.RULE,
            now=datetime(2026, 9, 18, 7, 0, tzinfo=UTC),
            source_asof=datetime(2026, 9, 18, 6, 30, tzinfo=UTC),
            calendar=KR,
        )
        assert provenance.freshness is Freshness.FRESH

    def test_skipping_a_session_is_stale(self) -> None:
        """Thursday's data on Monday means Friday's session was missed."""
        thursday = datetime(2026, 9, 17, 6, 30, tzinfo=UTC)
        monday = datetime(2026, 9, 21, 0, 30, tzinfo=UTC)

        provenance = evaluate_freshness(self.RULE, now=monday, source_asof=thursday, calendar=KR)

        assert provenance.freshness is Freshness.STALE

    def test_calendar_is_mandatory(self) -> None:
        with pytest.raises(ValueError, match="requires a calendar"):
            evaluate_freshness(
                self.RULE,
                now=datetime(2026, 9, 21, tzinfo=UTC),
                source_asof=datetime(2026, 9, 18, tzinfo=UTC),
            )


class TestNewsAndSocialFreshness:
    """Judged on the wall clock: this data is supposed to keep flowing."""

    RULE = WallClockFreshnessRule(max_age=timedelta(hours=6))

    def test_within_the_window_is_fresh(self) -> None:
        now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
        provenance = evaluate_freshness(self.RULE, now=now, source_asof=now - timedelta(hours=5))
        assert provenance.freshness is Freshness.FRESH

    def test_eight_hours_of_silence_is_stale(self) -> None:
        now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
        provenance = evaluate_freshness(self.RULE, now=now, source_asof=now - timedelta(hours=8))
        assert provenance.freshness is Freshness.STALE

    def test_weekends_do_not_excuse_social_silence(self) -> None:
        """Unlike market data, people post on Saturdays."""
        saturday = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
        provenance = evaluate_freshness(
            self.RULE, now=saturday, source_asof=saturday - timedelta(hours=9)
        )
        assert provenance.freshness is Freshness.STALE


class TestAvailabilityResolution:
    def test_fresh_data_keeps_its_requested_weight(self) -> None:
        provenance = evaluate_freshness(
            WallClockFreshnessRule(timedelta(hours=6)),
            now=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
            source_asof=datetime(2026, 9, 19, 11, 0, tzinfo=UTC),
        )

        verdict = resolve_availability(
            Engine.SENTIMENT,
            provenance=provenance,
            requested_weight=0.20,
            policy=MissingFactorPolicy.ABSTAIN,
            is_required=False,
        )

        assert verdict.availability is Availability.AVAILABLE
        assert verdict.effective_weight == 0.20
        assert verdict.reason is None

    def test_stale_factor_loses_its_weight_and_gains_a_reason(self) -> None:
        provenance = evaluate_freshness(
            WallClockFreshnessRule(timedelta(hours=6)),
            now=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
            source_asof=datetime(2026, 9, 19, 4, 0, tzinfo=UTC),
        )

        verdict = resolve_availability(
            Engine.SENTIMENT,
            provenance=provenance,
            requested_weight=0.20,
            policy=MissingFactorPolicy.ZERO,
            is_required=False,
        )

        assert verdict.availability is Availability.UNAVAILABLE
        assert verdict.effective_weight == 0.0
        assert verdict.reason is not None
        assert "8.0시간 전" in verdict.reason

    def test_stale_source_check_says_a_filing_may_have_been_missed(self) -> None:
        """The message should name the real risk, not just report a duration."""
        now = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)
        provenance = evaluate_freshness(
            SourceCheckFreshnessRule(timedelta(days=3)),
            now=now,
            source_asof=now - timedelta(days=36),
            source_checked_at=now - timedelta(days=10),
        )

        verdict = resolve_availability(
            Engine.FUNDAMENTAL,
            provenance=provenance,
            requested_weight=0.30,
            policy=MissingFactorPolicy.ZERO,
            is_required=False,
        )

        assert verdict.reason is not None
        assert "새 공시를 놓쳤을 수 있음" in verdict.reason


class TestRenormalization:
    def test_weights_are_redistributed_proportionally(self) -> None:
        requested = {
            Engine.TECHNICAL: 0.40,
            Engine.FUNDAMENTAL: 0.30,
            Engine.SENTIMENT: 0.20,
            Engine.PORTFOLIO: 0.10,
        }

        result = renormalized_weights(requested, unavailable={Engine.SENTIMENT})

        assert result[Engine.TECHNICAL] == pytest.approx(0.50)
        assert result[Engine.FUNDAMENTAL] == pytest.approx(0.375)
        assert result[Engine.SENTIMENT] == 0.0
        assert result[Engine.PORTFOLIO] == pytest.approx(0.125)
        assert sum(result.values()) == pytest.approx(1.0)

    def test_renormalizing_changes_the_strategy(self) -> None:
        """Documented as a test because it is the reason this is opt-in.

        40/30/20/10 becoming 50/37.5/0/12.5 is not the configured strategy
        running with one input missing — it is a different strategy. Runs that
        use it are not comparable with runs that did not.
        """
        requested = {
            Engine.TECHNICAL: 0.40,
            Engine.FUNDAMENTAL: 0.30,
            Engine.SENTIMENT: 0.20,
            Engine.PORTFOLIO: 0.10,
        }

        result = renormalized_weights(requested, unavailable={Engine.SENTIMENT})

        assert result[Engine.TECHNICAL] != requested[Engine.TECHNICAL]

    def test_everything_unavailable_yields_zeroes_not_a_crash(self) -> None:
        requested = {Engine.TECHNICAL: 0.5, Engine.FUNDAMENTAL: 0.5}
        result = renormalized_weights(requested, unavailable=set(requested))
        assert all(w == 0.0 for w in result.values())
