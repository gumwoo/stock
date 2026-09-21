"""Strategies the engine can run, and the definitions they are built from.

Kept deliberately plain. The interesting engineering in this project is the
point-in-time machinery, not the rule — and a rule elaborate enough to be
impressive is also elaborate enough to fit the sample it was written against.
A moving-average cross is something whose behaviour can be predicted by hand,
which is what a first backtest needs to be checkable at all.

**A strategy is built from its definition, never described alongside it.** A
live strategy object cannot be stored or compared, so something has to stand
in for it in a persisted run. If that stand-in is written by hand next to the
object, the two drift — and the drift is invisible, because the run works
either way:

    StrategySpec(
        version="ma-cross@v1",
        strategy=MovingAverageCross(short=10, long=30),
        params={"short": 20, "long": 60},   # what the row would have said
    )

Two specs could also share a version and behave differently. Neither breaks a
backtest today; both break the reproducibility a stored run is supposed to
have, which is the whole point of storing it.

`StrategyDefinition` removes the gap by making the stored form the *only*
source of the running object. The params are the constructor's arguments, so
what a row says and what ran cannot disagree without the build failing.

Every strategy here reads through the `MarketData` it is handed, already
positioned at the decision instant. None holds a database session, and none
can choose which moment to read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.backtest.engine import MarketData, Signal, Strategy
from app.core.indicators import simple_moving_average
from app.core.types import Interval


class UnknownStrategyError(Exception):
    """A definition names a kind or parameter this build cannot produce."""


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    """Everything needed to rebuild a strategy, and nothing else.

    This is what a `backtest_run` row holds. `build()` is the only way to get
    a running strategy from it, so the definition that was stored, the one
    that will be replayed and the one that actually ran are the same object by
    construction rather than by discipline.

    `version` must change whenever behaviour does. 20/60 and 10/30 produce
    different trades from identical data, so they are different strategies;
    the params make that visible even when someone forgets, since they are
    stored too and compared on replay.
    """

    kind: str
    version: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise UnknownStrategyError("a strategy definition needs a kind")
        if not self.version.strip():
            raise UnknownStrategyError("a strategy definition needs a version")

    def build(self) -> Strategy:
        """Construct the running strategy. Raises rather than guessing."""
        try:
            factory = _KINDS[self.kind]
        except KeyError:
            known = ", ".join(sorted(_KINDS))
            raise UnknownStrategyError(
                f"no strategy kind {self.kind!r}; this build knows {known}"
            ) from None
        try:
            built: Strategy = factory(**dict(self.params))
        except TypeError as exc:
            # A misspelled or missing parameter must fail loudly. Falling back
            # to a default would run something other than what the row says,
            # which is the failure this class exists to prevent.
            raise UnknownStrategyError(
                f"cannot build {self.kind} from {dict(self.params)}: {exc}"
            ) from exc
        return built

    def describe(self) -> str:
        """One line for a report or a log."""
        if not self.params:
            return f"{self.kind}@{self.version}"
        inner = " ".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.kind}@{self.version} ({inner})"


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


# The kinds a definition may name. A strategy absent from here cannot be
# stored, which is deliberate: an unrunnable row is worse than a rejected one.
_KINDS: dict[str, Any] = {
    "moving_average_cross": MovingAverageCross,
    "buy_and_hold": BuyAndHold,
}


def moving_average_cross(
    *, short: int = 20, long: int = 60, version: str | None = None
) -> StrategyDefinition:
    """A definition whose params are exactly the constructor's arguments."""
    MovingAverageCross(short=short, long=long)  # fail here, not at build time
    return StrategyDefinition(
        kind="moving_average_cross",
        version=version or f"ma-{short}-{long}@v1",
        params={"short": short, "long": long},
    )


def buy_and_hold(*, version: str = "buy-and-hold@v1") -> StrategyDefinition:
    return StrategyDefinition(kind="buy_and_hold", version=version, params={})
