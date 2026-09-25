"""The review gates and the pre-registered overlay rule."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.calendar import Market
from app.core.types import SignalAction
from app.scoring.review import (
    DECISION_GATE,
    EXTENDED_GATE,
    MIN_DAYS_PER_SIDE,
    Spread,
    age_bucket,
    by_age,
    by_day,
    decision_sample,
    overlay_verdict,
    spread,
)
from app.services import review_service
from app.services.forward_service import Record

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def day(n: int) -> datetime:
    return T0 + timedelta(days=n)


class TestByDay:
    def test_a_day_is_one_observation_however_many_names_it_has(self) -> None:
        # Day 0: ten names at +1. Day 1: one name at -1. Two days, mean 0.
        stats = by_day([(day(0), 1.0)] * 10 + [(day(1), -1.0)])
        assert (stats.days, stats.mean) == (2, 0.0)

    def test_standard_error_across_days(self) -> None:
        stats = by_day([(day(0), 1.0), (day(1), 3.0), (day(2), 5.0)])
        assert stats.mean == 3.0
        assert stats.se == pytest.approx(2.0 / math.sqrt(3))
        assert stats.t == pytest.approx(3.0 / (2.0 / math.sqrt(3)))

    def test_one_day_has_no_spread_and_none_has_no_mean(self) -> None:
        assert by_day([(day(0), 1.0)]).se is None
        assert by_day([]).mean is None


def series(
    n: int, value: float, *, start: int = 0, wobble: float = 0.5
) -> list[tuple[datetime, float]]:
    return [(day(start + i), value + (wobble if i % 2 else -wobble)) for i in range(n)]


def strong(n: int = 30, start: int = 0) -> Spread:
    return spread(series(n, 1.0, start=start), series(n, -1.0, start=start))


class TestVerdict:
    def test_not_ready_before_the_decision_gate(self) -> None:
        v = overlay_verdict(
            sample_days=None,
            entry_days=DECISION_GATE.days - 1,
            whole=strong(),
            halves=(strong(), strong()),
            risk_on=strong(),
            other_regimes=strong(),
        )
        assert (v.ready, v.passed) == (False, False)

    def test_too_little_news_in_the_decision_sample_is_a_final_no(self) -> None:
        thin = spread(series(MIN_DAYS_PER_SIDE - 1, 1.0), series(40, -1.0))
        v = overlay_verdict(
            sample_days=60,
            entry_days=80,
            whole=thin,
            halves=(strong(), strong()),
            risk_on=thin,
            other_regimes=thin,
        )
        assert (v.ready, v.passed) == (True, False)
        assert any("the answer is no" in r for r in v.reasons)

    def test_passes_when_every_condition_holds(self) -> None:
        v = overlay_verdict(
            sample_days=60,
            entry_days=80,
            whole=strong(),
            halves=(strong(15), strong(15, start=100)),
            risk_on=strong(10),
            other_regimes=strong(10),
        )
        assert (v.ready, v.passed) == (True, True)

    def test_a_weak_difference_fails(self) -> None:
        noisy = spread(series(30, 0.1, wobble=3.0), series(30, 0.0, wobble=3.0))
        v = overlay_verdict(
            sample_days=60,
            entry_days=80,
            whole=noisy,
            halves=(strong(), strong()),
            risk_on=strong(),
            other_regimes=strong(),
        )
        assert (v.ready, v.passed) == (True, False)

    def test_a_reversal_in_one_half_fails(self) -> None:
        reversed_half = spread(series(15, -1.0), series(15, 1.0))
        v = overlay_verdict(
            sample_days=60,
            entry_days=80,
            whole=strong(),
            halves=(strong(15), reversed_half),
            risk_on=strong(),
            other_regimes=strong(),
        )
        assert v.passed is False

    def test_an_untested_half_fails(self) -> None:
        empty = spread([], [])
        v = overlay_verdict(
            sample_days=60,
            entry_days=80,
            whole=strong(),
            halves=(strong(15), empty),
            risk_on=strong(),
            other_regimes=strong(),
        )
        assert v.passed is False

    def test_an_untested_regime_does_not_count_against(self) -> None:
        v = overlay_verdict(
            sample_days=60,
            entry_days=80,
            whole=strong(),
            halves=(strong(15), strong(15)),
            risk_on=strong(),
            other_regimes=spread([], []),
        )
        assert v.passed is True
        assert any("untested" in r for r in v.reasons)

    def test_a_reversal_in_one_regime_fails(self) -> None:
        v = overlay_verdict(
            sample_days=60,
            entry_days=80,
            whole=strong(),
            halves=(strong(15), strong(15)),
            risk_on=strong(),
            other_regimes=spread(series(10, -1.0), series(10, 1.0)),
        )
        assert v.passed is False


class TestDecisionSample:
    def test_nothing_is_due_before_sixty_days(self) -> None:
        assert decision_sample(DECISION_GATE.days - 1, {}) == (None, False)

    def test_the_first_sixty_days_when_they_hold_enough_news(self) -> None:
        assert decision_sample(75, {DECISION_GATE.days: True}) == (DECISION_GATE.days, True)

    def test_thin_news_waits_for_the_one_extension(self) -> None:
        assert decision_sample(90, {DECISION_GATE.days: False}) == (None, False)

    def test_the_extension_is_final_whatever_it_holds(self) -> None:
        got = decision_sample(130, {DECISION_GATE.days: False, EXTENDED_GATE.days: False})
        assert got == (EXTENDED_GATE.days, True)


class TestAge:
    @pytest.mark.parametrize(
        ("age", "label"),
        [(0.2, "under 1 day"), (1.0, "1-3 days"), (5.0, "3-7 days"), (30.0, "7+ days")],
    )
    def test_buckets(self, age: float, label: str) -> None:
        assert age_bucket(age) == label

    def test_an_event_after_the_judgement_is_left_out(self) -> None:
        assert by_age([(-0.5, day(0), 9.0)]) == {}

    def test_by_age_groups_by_day_within_each_bucket(self) -> None:
        got = by_age([(0.5, day(0), 2.0), (0.5, day(0), 4.0), (5.0, day(1), -1.0)])
        assert got["under 1 day"].mean == 3.0 and got["under 1 day"].days == 1
        assert got["3-7 days"].mean == -1.0


def record(
    n: int, points: float | None, excess: float, *, regime: str = "RISK_ON", detail: Any = None
) -> Record:
    entry = day(n)
    return Record(
        horizon=5,
        entry_at=entry,
        decision_at=entry - timedelta(hours=18),
        return_pct=excess,
        excess=excess,
        action=SignalAction.WATCH,
        market=Market.KR,
        overlay_points=points,
        overlay_detail=detail,
        regime=regime,
        attention_status=None,
        surge=None,
    )


class TestReview:
    def test_the_record_is_gathered_into_the_rule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        records = []
        for n in range(80):
            regime = "RISK_ON" if n % 2 else "NEUTRAL"
            records.append(record(n, 3.0, 1.0 + (0.3 if n % 3 else -0.3), regime=regime))
            records.append(record(n, -3.0, -1.0 + (0.3 if n % 3 else -0.3), regime=regime))
            records.append(record(n, 0.0, 0.0, regime=regime))
        monkeypatch.setattr(review_service, "signal_records", lambda s: (records, len(records)))

        rev = review_service.review(None, now=day(90))  # type: ignore[arg-type]

        assert rev.gates[0].reached and rev.gates[1].reached
        assert rev.gates[1].earliest is None
        # Decided on the first sixty entry days, not on all eighty.
        assert rev.spread is not None and rev.spread.good.days == 60
        assert rev.verdict is not None and (rev.verdict.ready, rev.verdict.passed) == (True, True)
        assert any("first half" in r and "held" in r for r in rev.verdict.reasons)
        # Both regime sides had enough days to be tested, not waved through.
        assert any(r.startswith("RISK_ON: difference") for r in rev.verdict.reasons)
        assert any(r.startswith("other regimes: difference") for r in rev.verdict.reasons)

    def test_the_half_life_reads_each_event_in_its_own_direction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bad news, a day old at the decision, followed by a fall: +2 in its direction.
        entry = day(0)
        first_at = (entry - timedelta(hours=18) - timedelta(days=1, hours=6)).isoformat()
        detail = [
            {"sentiment": -0.8, "first_at": first_at},
            {"sentiment": 0.0, "first_at": first_at},
        ]
        records = [record(0, -3.0, -2.0, detail=detail)]
        monkeypatch.setattr(review_service, "signal_records", lambda s: (records, 1))
        rev = review_service.review(None, now=day(10))  # type: ignore[arg-type]
        assert rev.half_life["1-3 days"].mean == 2.0
        assert "under 1 day" not in rev.half_life

    def test_before_any_record_the_gates_have_a_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(review_service, "signal_records", lambda s: ([], 0))
        rev = review_service.review(None, now=datetime(2026, 9, 25, 8, tzinfo=UTC))  # type: ignore[arg-type]
        first, second, extension = rev.gates
        assert not first.reached and first.earliest is not None
        assert second.earliest is not None and second.earliest > first.earliest
        assert extension.earliest is not None and extension.earliest > second.earliest
        assert rev.verdict is not None and rev.verdict.ready is False


class TestFixedSample:
    def _day(self, n: int, sign: float) -> list[Record]:
        regime = "RISK_ON" if n % 2 else "NEUTRAL"
        wobble = 0.3 if n % 3 else -0.3
        return [
            record(n, 3.0, sign * (1.0 + wobble), regime=regime),
            record(n, -3.0, sign * (-1.0 + wobble), regime=regime),
        ]

    def test_days_after_the_decision_sample_do_not_change_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sixty days where good news wins, then forty where it loses badly.
        records = [r for n in range(60) for r in self._day(n, 1.0)]
        records += [r for n in range(60, 100) for r in self._day(n, -5.0)]
        monkeypatch.setattr(review_service, "signal_records", lambda s: (records, len(records)))
        rev = review_service.review(None, now=day(120))  # type: ignore[arg-type]
        assert rev.verdict is not None and rev.verdict.passed is True
        assert rev.spread is not None and rev.spread.good.days == 60

    def test_only_korean_days_count_toward_the_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A US entry is a different instant of the same day: it must not add a day.
        us = [
            Record(
                horizon=5,
                entry_at=day(n) + timedelta(hours=13, minutes=30),
                decision_at=day(n),
                return_pct=0.0,
                excess=0.0,
                action=SignalAction.WATCH,
                market=Market.US,
                overlay_points=0.0,
                overlay_detail=None,
                regime="RISK_ON",
                attention_status=None,
                surge=None,
            )
            for n in range(30)
        ]
        kr = [record(n, 0.0, 0.0) for n in range(30)]
        monkeypatch.setattr(review_service, "signal_records", lambda s: (kr + us, 60))
        rev = review_service.review(None, now=day(40))  # type: ignore[arg-type]
        assert rev.gates[0].days == 30
        assert rev.gates[1].reached is False


def test_a_regime_not_known_is_not_counted_as_another_regime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every fourth day the regime could not be read, and news went the other way on those.
    records = []
    for n in range(60):
        unknown = n % 4 == 0
        sign = -0.25 if unknown else 1.0
        wobble = 0.3 if n % 3 else -0.3
        regime = "UNKNOWN" if unknown else "RISK_ON"
        records.append(record(n, 3.0, sign * 2.0 + wobble, regime=regime))
        records.append(record(n, -3.0, -sign * 2.0 + wobble, regime=regime))
    monkeypatch.setattr(review_service, "signal_records", lambda s: (records, len(records)))
    rev = review_service.review(None, now=day(80))  # type: ignore[arg-type]
    assert rev.verdict is not None and rev.verdict.passed is True
    assert any(r.startswith("other regimes: untested") for r in rev.verdict.reasons)
