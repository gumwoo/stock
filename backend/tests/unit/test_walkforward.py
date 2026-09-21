"""Splitting a history into train and evaluate periods.

The arithmetic is simple and the properties are what matter: evaluation
periods must tile the timeline exactly once, training must never reach into
the period it will be judged on, and the holdout must be unreachable rather
than merely undocumented.

A split that cannot be made must fail rather than come back empty. An empty
list reads downstream as "walk-forward ran and found nothing wrong", which is
the most flattering possible way to report that it never ran.
"""

from __future__ import annotations

from datetime import date
from itertools import pairwise

import pytest

from app.backtest.walkforward import (
    SampleType,
    WalkForwardError,
    evaluation_covers,
    generate,
)
from app.core.calendar import Market, MarketCalendar

US = MarketCalendar(Market.US)

# Two full years of real sessions, so every boundary below is a trading day.
SESSIONS = US.sessions_between(date(2024, 1, 2), date(2025, 12, 31))


class TestWindowLayout:
    def test_the_first_window_starts_at_the_beginning(self) -> None:
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)

        assert split.windows[0].train_start == SESSIONS[0]

    def test_training_ends_immediately_before_evaluation(self) -> None:
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)

        for window in split.windows:
            train_end_index = SESSIONS.index(window.train_end)
            assert SESSIONS[train_end_index + 1] == window.eval_start

    def test_evaluation_periods_tile_without_overlapping(self) -> None:
        """So out-of-sample results can be concatenated without double counting."""
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)

        spans = [(w.eval_start, w.eval_end) for w in split.windows]
        for (_, earlier_end), (later_start, _) in pairwise(spans):
            assert later_start > earlier_end

    def test_every_boundary_is_a_real_session(self) -> None:
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)

        for window in split.windows:
            for moment in (
                window.train_start,
                window.train_end,
                window.eval_start,
                window.eval_end,
            ):
                assert moment in SESSIONS

    def test_windows_are_indexed_in_order(self) -> None:
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)

        assert [w.index for w in split.windows] == list(range(len(split.windows)))

    def test_lengths_are_exact(self) -> None:
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)

        for window in split.windows:
            train = SESSIONS.index(window.train_end) - SESSIONS.index(window.train_start) + 1
            evaluate = SESSIONS.index(window.eval_end) - SESSIONS.index(window.eval_start) + 1
            assert (train, evaluate) == (100, 50)


class TestRollingVersusAnchored:
    def test_rolling_training_moves_forward(self) -> None:
        """An old regime eventually falls out, which is how decay shows up."""
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)

        starts = [w.train_start for w in split.windows]
        assert starts == sorted(starts)
        assert starts[0] < starts[-1]

    def test_anchored_training_always_starts_at_the_beginning(self) -> None:
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50, anchored=True)

        assert {w.train_start for w in split.windows} == {SESSIONS[0]}

    def test_anchored_training_grows(self) -> None:
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50, anchored=True)

        lengths = [SESSIONS.index(w.train_end) for w in split.windows]
        assert lengths == sorted(lengths)
        assert len(set(lengths)) == len(lengths)

    def test_both_produce_the_same_evaluation_periods(self) -> None:
        """Only the training side differs, so the results stay comparable."""
        rolling = generate(SESSIONS, train_sessions=100, eval_sessions=50)
        anchored = generate(SESSIONS, train_sessions=100, eval_sessions=50, anchored=True)

        assert [(w.eval_start, w.eval_end) for w in rolling.windows] == [
            (w.eval_start, w.eval_end) for w in anchored.windows
        ]


class TestHoldout:
    def test_it_is_reported(self) -> None:
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50, holdout_sessions=40)

        assert split.has_holdout
        assert split.holdout_start == SESSIONS[-40]
        assert split.holdout_end == SESSIONS[-1]

    def test_no_window_can_reach_it(self) -> None:
        """The only protection a holdout has is being unreachable."""
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50, holdout_sessions=40)
        assert split.holdout_start is not None

        for window in split.windows:
            assert window.eval_end < split.holdout_start
            assert window.train_end < split.holdout_start

    def test_reserving_it_can_cost_a_window(self) -> None:
        without = generate(SESSIONS, train_sessions=100, eval_sessions=50)
        with_holdout = generate(
            SESSIONS, train_sessions=100, eval_sessions=50, holdout_sessions=100
        )

        assert len(with_holdout.windows) < len(without.windows)

    def test_a_holdout_swallowing_everything_is_refused(self) -> None:
        with pytest.raises(WalkForwardError, match="leaves nothing"):
            generate(SESSIONS, train_sessions=10, eval_sessions=5, holdout_sessions=len(SESSIONS))

    def test_no_holdout_by_default(self) -> None:
        """It is a deliberate reservation, not something to half-have."""
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)

        assert not split.has_holdout


class TestRefusals:
    def test_too_little_history_raises_rather_than_returning_nothing(self) -> None:
        """An empty list reads as 'no overfitting found'."""
        with pytest.raises(WalkForwardError, match="needs 150 sessions"):
            generate(SESSIONS[:100], train_sessions=100, eval_sessions=50)

    def test_the_message_says_what_was_available(self) -> None:
        with pytest.raises(WalkForwardError) as caught:
            generate(SESSIONS[:100], train_sessions=100, eval_sessions=50)

        assert "only 100" in str(caught.value)

    def test_a_holdout_that_causes_the_shortfall_is_named(self) -> None:
        with pytest.raises(WalkForwardError, match="reserving 400"):
            generate(SESSIONS, train_sessions=100, eval_sessions=50, holdout_sessions=400)

    @pytest.mark.parametrize(("train", "evaluate"), [(0, 50), (100, 0), (-1, 50)])
    def test_empty_periods_are_refused(self, train: int, evaluate: int) -> None:
        with pytest.raises(WalkForwardError, match="at least one session"):
            generate(SESSIONS, train_sessions=train, eval_sessions=evaluate)

    def test_a_zero_step_is_refused(self) -> None:
        """It would generate the same window forever."""
        with pytest.raises(WalkForwardError, match="step must be"):
            generate(SESSIONS, train_sessions=100, eval_sessions=50, step_sessions=0)


class TestStep:
    """The step must equal the evaluation length, and that is enforced.

    Anything else breaks what `evaluation_covers` reports, in the flattering
    direction. Measured on these sessions with 120/60 windows:

        step 30   span 360 sessions, 660 observations — every day in the
                  overlap counted twice, invisible from the span
        step 90   span 330 sessions, 240 evaluated — 90 sessions inside the
                  reported range never measured, and nothing saying so

    A single pair of dates cannot express either, so those splits are refused
    rather than summarised misleadingly.
    """

    def test_the_default_step_tiles_the_evaluation_periods(self) -> None:
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)

        gaps = [
            SESSIONS.index(later.eval_start) - SESSIONS.index(earlier.eval_end)
            for earlier, later in pairwise(split.windows)
        ]
        assert set(gaps) == {1}

    def test_a_smaller_step_is_refused(self) -> None:
        """It would measure the overlap twice."""
        with pytest.raises(WalkForwardError, match="must equal eval_sessions"):
            generate(SESSIONS, train_sessions=100, eval_sessions=50, step_sessions=10)

    def test_a_larger_step_is_refused(self) -> None:
        """It would leave a hole inside the span it reports."""
        with pytest.raises(WalkForwardError, match="must equal eval_sessions"):
            generate(SESSIONS, train_sessions=100, eval_sessions=50, step_sessions=80)

    def test_stating_the_step_explicitly_is_allowed(self) -> None:
        stated = generate(SESSIONS, train_sessions=100, eval_sessions=50, step_sessions=50)
        implied = generate(SESSIONS, train_sessions=100, eval_sessions=50)

        assert stated == implied

    def test_every_session_in_the_span_is_evaluated_exactly_once(self) -> None:
        """The property the refusals exist to protect."""
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)

        measured = [
            index
            for w in split.windows
            for index in range(SESSIONS.index(w.eval_start), SESSIONS.index(w.eval_end) + 1)
        ]
        span = evaluation_covers(split)
        assert span is not None
        expected = range(SESSIONS.index(span[0]), SESSIONS.index(span[1]) + 1)

        assert measured == list(expected)
        assert len(measured) == len(set(measured))


class TestReportingTheSpan:
    def test_it_covers_first_to_last_evaluation(self) -> None:
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)
        span = evaluation_covers(split)

        assert span == (split.windows[0].eval_start, split.windows[-1].eval_end)

    def test_it_is_shorter_than_the_history(self) -> None:
        """The first training period is not out-of-sample, and saying so keeps
        a two-year split from being read as two years of evidence."""
        split = generate(SESSIONS, train_sessions=100, eval_sessions=50)
        span = evaluation_covers(split)
        assert span is not None

        assert span[0] > SESSIONS[0]


class TestSampleTypes:
    def test_the_three_are_distinct(self) -> None:
        assert len({SampleType.IN_SAMPLE, SampleType.OUT_OF_SAMPLE, SampleType.HOLDOUT}) == 3
