"""Strategies the engine can run.

Kept deliberately plain. The interesting engineering in this project is the
point-in-time machinery, not the rule — and a rule elaborate enough to be
impressive is also elaborate enough to fit the sample it was written against.
A moving-average cross is something whose behaviour can be predicted by hand,
which is what a first backtest needs to be checkable at all.

Every strategy here reads through the `MarketData` it is handed, already
positioned at the decision instant. None of them holds a database session, and
none can choose which moment to read.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.backtest.engine import MarketData, Signal
from app.core.indicators import simple_moving_average
from app.core.types import Interval


@dataclass(frozen=True, slots=True)
class MovingAverageCross:
    """Long while the short average sits above the long one, flat otherwise.

    `short` and `long` are part of the strategy version rather than constants.
    Changing 20/60 to 10/50 produces different trades from identical data,
    which makes it a strategy change and not a tweak.

    While history is too short for the long average, the strategy abstains
    rather than holding. The distinction matters at the start of every run: a
    HOLD there would be a judgement made from an average that does not exist,
    and the opening weeks of a backtest are exactly where such a judgement
    silently sets up the rest of the curve.
    """

    short: int = 20
    long: int = 60
    interval: Interval = Interval.DAY_1

    def __post_init__(self) -> None:
        if self.short >= self.long:
            raise ValueError(f"short ({self.short}) must be under long ({self.long})")

    def evaluate(self, data: MarketData, instrument_id: int) -> Signal:
        bars = data.bars(instrument_id, self.interval, limit=self.long)
        if len(bars) < self.long:
            return Signal.ABSTAIN

        closes = [float(b.close) for b in bars]
        fast = simple_moving_average(closes, self.short)
        slow = simple_moving_average(closes, self.long)
        if fast is None or slow is None:
            return Signal.ABSTAIN

        return Signal.ENTER if fast > slow else Signal.EXIT


@dataclass(frozen=True, slots=True)
class BuyAndHold:
    """Enter once, never leave. The benchmark every strategy is measured against.

    Not a toy. A strategy that trades all year to underperform this one has
    told you something, and without it in the same harness — same costs, same
    fill timing, same calendar — the comparison is against a number computed
    somewhere else under different rules.
    """

    interval: Interval = Interval.DAY_1

    def evaluate(self, data: MarketData, instrument_id: int) -> Signal:
        if not data.bars(instrument_id, self.interval, limit=1):
            return Signal.ABSTAIN
        return Signal.ENTER
