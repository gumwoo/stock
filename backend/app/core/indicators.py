"""Technical indicators as pure functions.

No database, no network, no pandas. Plain sequences in, plain values out.

That constraint is not minimalism for its own sake. These functions run inside
the backtest loop over historical windows, and they run again on live data in
exactly the same form. If they could reach for data themselves, the two paths
would drift, and a backtest would stop being evidence about the live system.
Being pure also makes them testable against published reference values, which
is the only way to know an RSI is actually an RSI.

Every function takes a series ordered oldest-first and returns the value at the
final element, or None when there is not enough history. Returning None rather
than a partial result is deliberate: a 14-period RSI computed from 6 bars is
not a slightly worse RSI, it is a different number wearing the same name.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import sqrt


def simple_moving_average(values: Sequence[float], period: int) -> float | None:
    """Mean of the last `period` values."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def exponential_moving_average(values: Sequence[float], period: int) -> float | None:
    """EMA seeded with the SMA of the first `period` values.

    Seeding from the SMA rather than the first observation is the convention
    used by most charting packages; starting from a single value makes the
    early series depend heavily on one arbitrary bar.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = (value - ema) * multiplier + ema
    return ema


def relative_strength_index(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI.

    Uses Wilder's smoothing (an EMA with alpha = 1/period), not a simple mean
    of gains and losses. The two differ by enough to change a signal near a
    threshold, and Wilder's is what "RSI 14" means to anyone reading the number.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period + 1:
        return None

    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        # No downward movement at all in the window: maximally overbought.
        return 100.0 if avg_gain > 0.0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass(frozen=True, slots=True)
class MacdResult:
    macd: float
    signal: float
    histogram: float


def macd(
    values: Sequence[float],
    *,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> MacdResult | None:
    """MACD line, signal line and histogram."""
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow period")
    if len(values) < slow + signal_period:
        return None

    macd_series: list[float] = []
    for end in range(slow, len(values) + 1):
        window = values[:end]
        fast_ema = exponential_moving_average(window, fast)
        slow_ema = exponential_moving_average(window, slow)
        if fast_ema is None or slow_ema is None:
            continue
        macd_series.append(fast_ema - slow_ema)

    if len(macd_series) < signal_period:
        return None

    macd_line = macd_series[-1]
    signal_line = exponential_moving_average(macd_series, signal_period)
    if signal_line is None:
        return None

    return MacdResult(
        macd=macd_line,
        signal=signal_line,
        histogram=macd_line - signal_line,
    )


@dataclass(frozen=True, slots=True)
class BollingerBands:
    upper: float
    middle: float
    lower: float

    def position(self, price: float) -> float:
        """Where `price` sits in the band, 0.0 at the lower, 1.0 at the upper.

        Values outside 0-1 mean the price has broken out of the band, which is
        information worth keeping rather than clamping away.
        """
        span = self.upper - self.lower
        if span == 0.0:
            return 0.5
        return (price - self.lower) / span


def bollinger_bands(
    values: Sequence[float], period: int = 20, num_std: float = 2.0
) -> BollingerBands | None:
    """Bollinger bands using the population standard deviation."""
    if len(values) < period:
        return None

    window = values[-period:]
    middle = sum(window) / period
    variance = sum((v - middle) ** 2 for v in window) / period
    deviation = sqrt(variance) * num_std

    return BollingerBands(
        upper=middle + deviation,
        middle=middle,
        lower=middle - deviation,
    )


def zscore(values: Sequence[float], period: int) -> float | None:
    """How many standard deviations the latest value sits from its recent mean.

    Used for volume, where the absolute number is meaningless across
    instruments — Samsung trades millions of shares and a small-cap trades
    thousands — but "unusual for this instrument" compares cleanly.
    """
    if period <= 1:
        raise ValueError("period must be greater than 1")
    if len(values) < period:
        return None

    window = values[-period:]
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    if variance == 0.0:
        return 0.0
    return (values[-1] - mean) / sqrt(variance)


def percent_distance(price: float, reference: float) -> float | None:
    """Percentage gap from `reference` to `price`.

    Returns a percentage, so +4.2 means the price is 4.2% above the reference.
    """
    if reference == 0.0:
        return None
    return (price - reference) / reference * 100.0
