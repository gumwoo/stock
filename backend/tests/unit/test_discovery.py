"""Surge arithmetic: rates over the time actually read, never raw counts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.scoring.discovery import MentionCounts, covered_days, inside, rank, surge

T = datetime(2026, 9, 23, tzinfo=UTC)
H = timedelta(hours=1)


class TestCoveredDays:
    def test_overlapping_sweeps_are_counted_once(self) -> None:
        """The watermark reaches back past the last run, so sweeps overlap."""
        spans = [(T, T + 12 * H), (T + 10 * H, T + 24 * H)]
        assert covered_days(spans, T, T + 48 * H) == pytest.approx(1.0)

    def test_gaps_are_not_covered(self) -> None:
        """A busy name's pages cover a few hours each, with gaps between."""
        spans = [(T, T + 3 * H), (T + 12 * H, T + 15 * H)]
        assert covered_days(spans, T, T + 24 * H) == pytest.approx(0.25)

    def test_only_the_asked_window_counts(self) -> None:
        assert covered_days([(T - 24 * H, T + 6 * H)], T, T + 24 * H) == pytest.approx(0.25)

    def test_nothing_read_is_zero(self) -> None:
        assert covered_days([], T, T + 24 * H) == 0.0

    def test_a_moment_is_inside_a_read_stretch_or_not(self) -> None:
        spans = [(T, T + 3 * H)]
        assert inside(T + H, spans)
        assert not inside(T + 4 * H, spans)


def counts(
    recent: int, baseline: int, *, recent_days: float = 1.0, baseline_days: float = 10.0, i: int = 1
) -> MentionCounts:
    return MentionCounts(
        instrument_id=i,
        recent=recent,
        baseline=baseline,
        recent_days=recent_days,
        baseline_days=baseline_days,
    )


class TestSurge:
    def test_rates_not_counts(self) -> None:
        """A hundred articles in four hours read is not a hundred in a day."""
        s = surge(counts(10, 30, recent_days=0.25, baseline_days=3.0))
        assert s.expected == pytest.approx(2.5)
        assert s.score == pytest.approx(11 / 3.5)

    def test_smoothing(self) -> None:
        assert surge(counts(1, 0)).score == pytest.approx(2.0)
        assert surge(counts(10, 10)).score == pytest.approx(5.5)

    def test_an_unread_window_is_refused(self) -> None:
        with pytest.raises(ValueError):
            surge(counts(5, 5, baseline_days=0.0))


class TestRank:
    def test_a_surge_beats_a_steady_large_count(self) -> None:
        rising = counts(6, 2, i=1)
        steady = counts(40, 400, i=2)
        top, _ = rank(
            [steady, rising], min_recent=3, min_recent_days=0.25, min_baseline_days=1.0, top=5
        )
        assert [s.instrument_id for s in top] == [1, 2]

    def test_too_little_read_is_unmeasured_not_quiet(self) -> None:
        barely = counts(50, 0, baseline_days=0.1, i=1)
        top, unmeasured = rank(
            [barely], min_recent=3, min_recent_days=0.25, min_baseline_days=1.0, top=5
        )
        assert (top, unmeasured) == ([], 1)

    def test_a_stray_article_is_not_a_story(self) -> None:
        top, unmeasured = rank(
            [counts(2, 0)], min_recent=3, min_recent_days=0.25, min_baseline_days=1.0, top=5
        )
        assert (top, unmeasured) == ([], 0)

    def test_the_list_is_cut_at_top(self) -> None:
        many = [counts(10 + n, 0, i=n) for n in range(10)]
        top, _ = rank(many, min_recent=3, min_recent_days=0.25, min_baseline_days=1.0, top=3)
        assert [s.instrument_id for s in top] == [9, 8, 7]


class TestPromotionRunsKeepTheirOwnName:
    """A run for three candidates must not read as a check of every tracked name."""

    def test_both_fetches_are_recorded_under_a_promotion_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from app.models.collector import CollectorStatus
        from app.services import promotion_service

        names: list[str] = []

        def fake_run(collector: object, session: object) -> SimpleNamespace:
            names.append(collector.name)  # type: ignore[attr-defined]
            return SimpleNamespace(status=CollectorStatus.SUCCESS)

        monkeypatch.setattr(promotion_service, "run_collector", fake_run)
        promotion_service.fetch_prices(None, [1])  # type: ignore[arg-type]
        promotion_service.fetch_fundamentals(None, [1])  # type: ignore[arg-type]

        assert names == ["PROMOTE_YFINANCE_HISTORY", "PROMOTE_DART_FUNDAMENTAL"]
        # The freshness lookup matches by prefix; neither may start like a real run.
        assert not any(n.startswith(("DART", "YFINANCE")) for n in names)
