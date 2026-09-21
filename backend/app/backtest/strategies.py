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

from dataclasses import dataclass
from typing import Any

from app.backtest.engine import MarketData, Signal, Strategy
from app.backtest.scoring_strategy import TechnicalFundamental
from app.core.indicators import simple_moving_average
from app.core.types import Interval, StrategyDefinition, UnknownStrategyError
from app.scoring.policy import STRATEGY_VERSION


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
    "technical_fundamental": TechnicalFundamental,
}


def build(definition: StrategyDefinition) -> Strategy:
    """Construct the running strategy a definition describes.

    A function rather than a method on the definition, because the registry is
    a property of this layer while the definition is a plain value. Keeping
    them apart is what lets `app.models` and `app.repositories` name a stored
    strategy without reaching up into the engines that run it.

    Raises rather than guessing. A misspelled or missing parameter falling
    back to a default would run something other than what the row says, which
    is the failure the definition exists to prevent.
    """
    try:
        factory = _KINDS[definition.kind]
    except KeyError:
        known = ", ".join(sorted(_KINDS))
        raise UnknownStrategyError(
            f"no strategy kind {definition.kind!r}; this build knows {known}"
        ) from None
    try:
        built: Strategy = factory(**dict(definition.params))
    except TypeError as exc:
        raise UnknownStrategyError(
            f"cannot build {definition.kind} from {dict(definition.params)}: {exc}"
        ) from exc
    return built


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


def technical_fundamental(
    *,
    buy_interest: float = 70.0,
    caution: float = 35.0,
    currency: str = "KRW",
    version: str | None = None,
) -> StrategyDefinition:
    """The system's own rule, as a stored definition.

    The version defaults to the shared policy's, because that is what decides
    the score — a change to the weights or the engines is a change to this
    strategy even though none of these parameters moved. The thresholds are
    parameters because turning a score into a position is this strategy's
    decision rather than the scorer's.
    """
    return StrategyDefinition(
        kind="technical_fundamental",
        version=version or f"{STRATEGY_VERSION}+{buy_interest:g}/{caution:g}",
        params={"buy_interest": buy_interest, "caution": caution, "currency": currency},
    )
