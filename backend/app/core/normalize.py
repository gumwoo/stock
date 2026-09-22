"""Turning raw measurements into comparable scores.

Pure functions, which is why this sits in `core` beside `indicators` rather
than in `scoring`: engines need it, and the layering contract places `scoring`
above `engines`. Policy about *which* normalization a strategy uses stays in
`scoring`; the arithmetic itself is domain math.

An RSI of 80 and an ROE of 80% are not the same quantity, and averaging them
directly is meaningless arithmetic that happens to produce a number. Everything
therefore passes through normalization to a 0-100 position before it is
weighted.

**Both scales are in use, and which one ran is recorded.** `percentile_rank`
answers "how does this instrument compare to its peers right now", which stays
meaningful when the market regime shifts. A time-series z-score would answer
"how unusual is this versus its own history", which sounds equivalent but
silently changes meaning when volatility regimes change: a 2-sigma move in a
calm market and in a panic are not comparable events.

The four monotonic fundamental ratios are ranked - ROE, debt ratio, operating
margin and revenue growth - each against the same market's universe at the same
instant. Everything else is mapped onto a fixed scale by `bounded` or
`peak_at`, because ranking it would assert something the strategy does not
believe: that the cheapest P/E in a market is the best one, or that an RSI is
overbought relative to other instruments rather than to 100.

`bounded` also serves as the stated fallback when a population is too thin to
rank against. It is a separate function rather than a branch inside percentile
so a fixed opinion can never be mistaken for a comparison, and the caller
records which of the two it used in the metric's own `detail`.
"""

from __future__ import annotations

from collections.abc import Sequence


def percentile_rank(value: float, population: Sequence[float]) -> float:
    """Where `value` sits within `population`, as 0-100.

    Uses the midpoint convention for ties: an instrument tied with others gets
    the average of the ranks that tie spans, so a universe of identical values
    scores 50 rather than 0 or 100.
    """
    if not population:
        raise ValueError("population must not be empty")

    below = sum(1 for p in population if p < value)
    equal = sum(1 for p in population if p == value)
    return (below + 0.5 * equal) / len(population) * 100.0


def bounded(value: float, low: float, high: float, *, invert: bool = False) -> float:
    """Map `value` from the range [low, high] onto 0-100, clamping outside it.

    For measures with a genuinely fixed scale, such as RSI, or when the
    universe is too small for ranking to mean anything.

    Args:
        invert: score high values as low. Use where a large raw number is a
            worse outcome, such as a debt ratio.
    """
    if high == low:
        raise ValueError("low and high must differ")

    position = (value - low) / (high - low)
    position = max(0.0, min(1.0, position))
    if invert:
        position = 1.0 - position
    return position * 100.0


def peak_at(value: float, ideal: float, tolerance: float) -> float:
    """Score highest at `ideal`, falling off linearly in both directions.

    For measures where both extremes are bad. RSI is the obvious case: 50 is
    unremarkable, 85 is overbought and 15 is oversold, and a monotonic mapping
    would score one of those extremes as excellent.
    """
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")

    distance = abs(value - ideal)
    return max(0.0, 1.0 - distance / tolerance) * 100.0


def clamp_score(value: float) -> float:
    """Force a value into 0-100.

    A last line of defence before constructing a `Metric`, whose validation
    would otherwise reject the row. Used where a formula is bounded in theory
    but floating point can step a hair outside.
    """
    return max(0.0, min(100.0, value))
