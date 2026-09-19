"""Indicator correctness against known values.

An indicator that is subtly wrong is worse than one that is missing: it
produces plausible numbers that quietly shift every signal near a threshold.
So these are checked against published reference data rather than against
whatever the implementation happens to produce.

The RSI series is Wilder's own worked example from *New Concepts in Technical
Trading Systems*, which is the canonical test case for a 14-period RSI.
"""

from __future__ import annotations

import pytest

from app.core.indicators import (
    bollinger_bands,
    exponential_moving_average,
    macd,
    percent_distance,
    relative_strength_index,
    simple_moving_average,
    zscore,
)

# Wilder's published example series, at full precision. Rounding these to two
# decimals shifts the resulting RSI by about 0.07, which is enough to matter
# near a threshold — so the reference data is kept exactly as published.
WILDER_CLOSES = [
    44.3389,
    44.0902,
    44.1497,
    43.6124,
    44.3278,
    44.8264,
    45.0955,
    45.4245,
    45.8433,
    46.0826,
    45.8931,
    46.0328,
    45.6140,
    46.2820,
    46.2820,
]


class TestSimpleMovingAverage:
    def test_known_value(self) -> None:
        assert simple_moving_average([1, 2, 3, 4, 5], 5) == 3.0

    def test_uses_only_the_last_period(self) -> None:
        assert simple_moving_average([100, 200, 1, 2, 3], 3) == 2.0

    def test_insufficient_history_returns_none(self) -> None:
        """None, not a partial average — a 5-period mean of 3 bars is not an SMA."""
        assert simple_moving_average([1, 2, 3], 5) is None

    def test_rejects_nonsense_period(self) -> None:
        with pytest.raises(ValueError, match="period must be positive"):
            simple_moving_average([1, 2, 3], 0)


class TestExponentialMovingAverage:
    def test_flat_series_equals_its_value(self) -> None:
        assert exponential_moving_average([5.0] * 20, 10) == pytest.approx(5.0)

    def test_reacts_faster_than_sma_to_a_recent_shift(self) -> None:
        """EMA leads SMA when the trend *changes*, not when it merely continues.

        Worth stating precisely, because the obvious test is wrong: on a
        constant-slope ramp both settle to the same lag of (period-1)/2 and the
        two are exactly equal. The advantage only appears at an inflection.
        """
        after_a_jump = [10.0] * 30 + [20.0] * 3

        ema = exponential_moving_average(after_a_jump, 10)
        sma = simple_moving_average(after_a_jump, 10)

        assert ema is not None and sma is not None
        assert ema > sma

    def test_matches_sma_on_a_constant_slope(self) -> None:
        """The counterpart: no lead when the slope never changes."""
        ramp = [float(i) for i in range(1, 101)]

        ema = exponential_moving_average(ramp, 10)
        sma = simple_moving_average(ramp, 10)

        assert ema is not None and sma is not None
        assert ema == pytest.approx(sma)

    def test_insufficient_history_returns_none(self) -> None:
        assert exponential_moving_average([1.0, 2.0], 10) is None


class TestRelativeStrengthIndex:
    def test_matches_wilders_published_example(self) -> None:
        """The canonical check: Wilder's own series gives 70.5327."""
        assert relative_strength_index(WILDER_CLOSES, 14) == pytest.approx(70.5327, abs=0.001)

    def test_monotonic_rise_is_maximally_overbought(self) -> None:
        assert relative_strength_index([float(i) for i in range(1, 31)], 14) == 100.0

    def test_monotonic_fall_is_maximally_oversold(self) -> None:
        rsi = relative_strength_index([float(i) for i in range(30, 0, -1)], 14)
        assert rsi == pytest.approx(0.0)

    def test_flat_series_is_neutral(self) -> None:
        """No movement in either direction is 50, not 0 or 100."""
        assert relative_strength_index([10.0] * 30, 14) == 50.0

    def test_needs_one_more_bar_than_the_period(self) -> None:
        """RSI works on deltas, so 14 periods needs 15 closes."""
        assert relative_strength_index([1.0] * 14, 14) is None
        assert relative_strength_index([1.0] * 15, 14) is not None

    def test_stays_within_bounds(self) -> None:
        noisy = [100.0, 102.0, 99.0, 105.0, 103.0, 98.0, 101.0, 107.0] * 5
        rsi = relative_strength_index(noisy, 14)
        assert rsi is not None
        assert 0.0 <= rsi <= 100.0


class TestMacd:
    def test_rising_series_gives_positive_macd(self) -> None:
        result = macd([float(i) for i in range(1, 80)])
        assert result is not None
        assert result.macd > 0

    def test_falling_series_gives_negative_macd(self) -> None:
        result = macd([float(i) for i in range(80, 1, -1)])
        assert result is not None
        assert result.macd < 0

    def test_histogram_is_macd_minus_signal(self) -> None:
        result = macd([float(i) for i in range(1, 80)])
        assert result is not None
        assert result.histogram == pytest.approx(result.macd - result.signal)

    def test_flat_series_gives_zero(self) -> None:
        result = macd([50.0] * 80)
        assert result is not None
        assert result.macd == pytest.approx(0.0, abs=1e-9)

    def test_insufficient_history_returns_none(self) -> None:
        assert macd([float(i) for i in range(1, 20)]) is None

    def test_rejects_inverted_periods(self) -> None:
        with pytest.raises(ValueError, match="fast period must be shorter"):
            macd([1.0] * 100, fast=26, slow=12)


class TestBollingerBands:
    def test_flat_series_collapses_the_band(self) -> None:
        bands = bollinger_bands([10.0] * 20, 20)
        assert bands is not None
        assert bands.upper == bands.middle == bands.lower == 10.0

    def test_bands_straddle_the_mean(self) -> None:
        bands = bollinger_bands([float(i) for i in range(1, 21)], 20)
        assert bands is not None
        assert bands.lower < bands.middle < bands.upper

    def test_position_maps_the_band_to_zero_and_one(self) -> None:
        bands = bollinger_bands([float(i) for i in range(1, 21)], 20)
        assert bands is not None
        assert bands.position(bands.lower) == pytest.approx(0.0)
        assert bands.position(bands.middle) == pytest.approx(0.5)
        assert bands.position(bands.upper) == pytest.approx(1.0)

    def test_breakout_reports_beyond_the_band(self) -> None:
        """Outside 0-1 is information, not an error to be clamped away."""
        bands = bollinger_bands([float(i) for i in range(1, 21)], 20)
        assert bands is not None
        assert bands.position(bands.upper * 2) > 1.0

    def test_collapsed_band_reports_the_midpoint(self) -> None:
        bands = bollinger_bands([10.0] * 20, 20)
        assert bands is not None
        assert bands.position(10.0) == 0.5


class TestZScore:
    def test_flat_series_is_zero(self) -> None:
        assert zscore([5.0] * 20, 20) == 0.0

    def test_spike_is_strongly_positive(self) -> None:
        """Volume's purpose: 'unusual for this instrument', not a raw count."""
        series = [100.0] * 19 + [500.0]
        result = zscore(series, 20)
        assert result is not None
        assert result > 3.0

    def test_scale_invariance_across_instruments(self) -> None:
        """A large-cap and a small-cap with the same relative spike score alike."""
        large = [1_000_000.0] * 19 + [1_400_000.0]
        small = [1_000.0] * 19 + [1_400.0]
        assert zscore(large, 20) == pytest.approx(zscore(small, 20))

    def test_rejects_degenerate_period(self) -> None:
        with pytest.raises(ValueError, match="greater than 1"):
            zscore([1.0, 2.0], 1)


class TestPercentDistance:
    def test_above_reference_is_positive(self) -> None:
        assert percent_distance(104.2, 100.0) == pytest.approx(4.2)

    def test_below_reference_is_negative(self) -> None:
        assert percent_distance(95.0, 100.0) == pytest.approx(-5.0)

    def test_zero_reference_returns_none_rather_than_dividing(self) -> None:
        assert percent_distance(100.0, 0.0) is None
