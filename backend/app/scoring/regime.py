"""What kind of market a judgement was made in: trend, volatility and breadth.

Pure. It takes closes, oldest first, that were all knowable at the moment in
question, and says:

- **trend** — the index's close against its 200-session average. Above it is
  the ordinary definition of a market that has been rising for most of a year.
- **volatility** — the annualised spread of the last 20 daily log returns, and
  the share of the 249 sessions before it where the same measure was lower. The rank
  rather than the level: 20% is calm for KOSDAQ and wild for the S&P 500.
- **breadth** — the share of names above their own 50-session average. It is
  recorded, not used in the label: it is measured over the names this system
  tracks, a few dozen, which says something about this universe and little
  about the market.

The label combines the first two. Rising and not unusually volatile is
RISK_ON; falling and unusually volatile is RISK_OFF; anything else is NEUTRAL.
Without enough history to know, UNKNOWN — not a guess.

**The thresholds are priors, not measurements.** 200 sessions, 20 sessions
and the 80th percentile are the conventional choices, not ones this system has
tested. The forward record grouped by regime is what may later say whether
the grouping means anything. So the parameters carry a version, stored with
every row.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

TRADING_DAYS = 252


class Label:
    RISK_ON = "RISK_ON"
    NEUTRAL = "NEUTRAL"
    RISK_OFF = "RISK_OFF"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RegimeParams:
    version: int = 1
    trend_window: int = 200
    return_window: int = 20
    volatility_window: int = 20
    rank_window: int = 250
    high_volatility_rank: float = 0.8
    breadth_window: int = 50

    @property
    def history_needed(self) -> int:
        """Closes needed for every measure: the rank looks back over a year of volatilities."""
        return max(self.trend_window, self.rank_window + self.volatility_window) + 1


DEFAULT = RegimeParams()


@dataclass(frozen=True, slots=True)
class Regime:
    label: str
    close: float | None = None
    trend_gap: float | None = None
    return_20d: float | None = None
    volatility: float | None = None
    volatility_rank: float | None = None


def _volatility(closes: Sequence[float]) -> float:
    """Annualised standard deviation of the log returns across `closes`."""
    returns = [math.log(b / a) for a, b in pairwise(closes)]
    return statistics.stdev(returns) * math.sqrt(TRADING_DAYS)


def classify(closes: Sequence[float], params: RegimeParams = DEFAULT) -> Regime:
    """The regime at the last close. `closes` oldest first, every one positive."""
    if len(closes) < params.history_needed or any(c <= 0 for c in closes):
        return Regime(Label.UNKNOWN, close=closes[-1] if closes else None)

    close = closes[-1]
    average = statistics.fmean(closes[-params.trend_window :])
    trend_gap = close / average - 1
    return_20d = close / closes[-1 - params.return_window] - 1

    w = params.volatility_window
    # The volatility at each of the past `rank_window` sessions, today's last.
    history = [
        _volatility(closes[end - w - 1 : end])
        for end in range(len(closes) - params.rank_window + 1, len(closes) + 1)
    ]
    volatility = history[-1]
    # The share of the past year it exceeds. Strictly: a year of unchanging
    # volatility is ordinary, not the highest on record.
    past = history[:-1]
    rank = sum(1 for v in past if v < volatility) / len(past)

    rising = trend_gap > 0
    turbulent = rank >= params.high_volatility_rank
    if rising and not turbulent:
        label = Label.RISK_ON
    elif not rising and turbulent:
        label = Label.RISK_OFF
    else:
        label = Label.NEUTRAL
    return Regime(label, close, trend_gap, return_20d, volatility, rank)


def breadth(
    closes_by_name: Sequence[Sequence[float]], params: RegimeParams = DEFAULT
) -> tuple[float | None, int]:
    """Share of names whose last close is above their own average, and how many were measured.

    A name with less history than the window is not measured rather than
    counted either way.
    """
    measured = [c for c in closes_by_name if len(c) >= params.breadth_window]
    if not measured:
        return None, 0
    above = sum(1 for c in measured if c[-1] > statistics.fmean(c[-params.breadth_window :]))
    return above / len(measured), len(measured)
