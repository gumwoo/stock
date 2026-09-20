"""Performance metrics, computed from an equity curve and a trade list.

Pure functions over plain values. No session, no ORM, no pandas — CI enforces
the first two and the third is a choice: these are a few dozen lines of
arithmetic whose correctness should be readable, and a metric that quietly
reindexes or forward-fills is a metric that reports a number nobody can check.

Every figure here is `None` rather than a plausible number when the sample
cannot support one. A Sharpe ratio computed from three observations is not a
small-sample Sharpe ratio, it is noise with a decimal point — and a run that
reports it will be compared against one that earned it. This project's
recurring failure mode is a figure that looks entirely normal and means
nothing, so the sample size travels with the result and the UI is expected to
say "not enough data" rather than print a number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

# Trading days per year, for annualising a daily series. KRX and NYSE both sit
# near this; the difference between 245 and 252 moves an annualised figure by
# far less than the uncertainty it already carries.
TRADING_DAYS = 252

# Below this many daily returns, dispersion-based figures are not reported.
MIN_OBSERVATIONS = 20


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Portfolio value at the close of one simulated day."""

    day: date
    value: Decimal


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """A round trip, after costs."""

    instrument_id: int
    entry_at: date
    exit_at: date
    pnl: Decimal


@dataclass(frozen=True, slots=True)
class Performance:
    """What a run achieved.

    `observations` is part of the result, not diagnostics. Every `None` below
    means "this sample cannot support that figure", and the reader needs to
    know how far from supporting it the sample was.
    """

    start: date
    end: date
    observations: int
    total_return: float | None
    cagr: float | None
    max_drawdown: float | None
    sharpe: float | None
    win_rate: float | None
    profit_factor: float | None
    trades: int

    @property
    def is_reportable(self) -> bool:
        """Whether the dispersion figures were computed at all."""
        return self.observations >= MIN_OBSERVATIONS


def daily_returns(curve: list[EquityPoint]) -> list[float]:
    """Simple returns between consecutive points.

    A day whose starting value is zero or negative yields no return: the
    portfolio is wiped out and percentage change stops being defined. Such
    days are dropped rather than clamped, because a clamped zero would be
    counted as a calm day and would flatter every dispersion figure below.
    """
    out: list[float] = []
    for previous, current in pairwise(curve):
        if previous.value <= 0:
            continue
        out.append(float(current.value / previous.value) - 1.0)
    return out


def total_return(curve: list[EquityPoint]) -> float | None:
    if len(curve) < 2 or curve[0].value <= 0:
        return None
    return float(curve[-1].value / curve[0].value) - 1.0


def cagr(curve: list[EquityPoint]) -> float | None:
    """Compound annual growth rate over the curve's calendar span.

    Measured in calendar days, not trading days: a strategy that was flat for
    a year was flat for a year, whether or not the market was open.

    Not reported for spans under a month, where annualising multiplies a few
    days of noise into a headline figure.
    """
    if len(curve) < 2 or curve[0].value <= 0 or curve[-1].value <= 0:
        return None
    days = (curve[-1].day - curve[0].day).days
    if days < 30:
        return None
    growth = float(curve[-1].value / curve[0].value)
    return float(growth ** (365.25 / days)) - 1.0


def max_drawdown(curve: list[EquityPoint]) -> float | None:
    """Largest peak-to-trough fall, as a negative fraction.

    Computed on the curve itself rather than on returns, so it reflects what
    the account actually went through rather than the worst run of days.
    """
    if len(curve) < 2:
        return None
    peak = curve[0].value
    worst = 0.0
    for point in curve:
        if point.value > peak:
            peak = point.value
        if peak > 0:
            drop = float(point.value / peak) - 1.0
            worst = min(worst, drop)
    return worst


def sharpe(returns: list[float], *, risk_free_annual: float = 0.0) -> float | None:
    """Annualised Sharpe ratio, or None when the sample is too small.

    The risk-free rate is de-annualised geometrically and subtracted per day,
    which matters once rates are not near zero: at 4% a year, treating it as
    zero adds roughly 0.2 to a typical ratio for free.
    """
    if len(returns) < MIN_OBSERVATIONS:
        return None

    daily_rf = (1.0 + risk_free_annual) ** (1.0 / TRADING_DAYS) - 1.0
    excess = [r - daily_rf for r in returns]

    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    if variance <= 0:
        # A perfectly constant series has no risk to divide by. Infinity is
        # not the answer; the ratio is simply undefined.
        return None
    return float(mean / math.sqrt(variance) * math.sqrt(TRADING_DAYS))


def win_rate(trades: list[ClosedTrade]) -> float | None:
    """Fraction of round trips that made money.

    Break-even trades count as losses. Splitting them out would let a strategy
    that mostly scratches report a flattering rate, and a trade that paid its
    costs and returned nothing did not win.
    """
    if not trades:
        return None
    wins = sum(1 for t in trades if t.pnl > 0)
    return wins / len(trades)


def profit_factor(trades: list[ClosedTrade]) -> float | None:
    """Gross profit divided by gross loss.

    None when there are no losing trades: the ratio is unbounded, and a run
    with two winners and no losers would otherwise print an infinity that
    reads as skill.
    """
    if not trades:
        return None
    gains = sum(float(t.pnl) for t in trades if t.pnl > 0)
    losses = -sum(float(t.pnl) for t in trades if t.pnl < 0)
    if losses <= 0:
        return None
    return gains / losses


def summarise(
    curve: list[EquityPoint],
    trades: list[ClosedTrade],
    *,
    risk_free_annual: float = 0.0,
) -> Performance | None:
    """Every figure a run reports, with its sample size attached."""
    if len(curve) < 2:
        return None

    returns = daily_returns(curve)
    return Performance(
        start=curve[0].day,
        end=curve[-1].day,
        observations=len(returns),
        total_return=total_return(curve),
        cagr=cagr(curve),
        max_drawdown=max_drawdown(curve),
        sharpe=sharpe(returns, risk_free_annual=risk_free_annual),
        win_rate=win_rate(trades),
        profit_factor=profit_factor(trades),
        trades=len(trades),
    )
