"""What one stock did through one session, from its one-minute bars.

Pure. Takes a day's bars in time order (09:00 to 15:30, minutes without a
trade simply absent) and measures:

- open to close, from the first bar's open to the last bar's close
- **MFE and MAE** — the highest high and the lowest low against the open.
  These are hindsight: nobody knows the high while it is being made, so they
  are how much room there was, not a return anyone could have taken.
- when the high and the low came, and how long after the open the high was
- a volume-weighted average price over the day, and where the close stood
  against it (bars priced at their typical price, (high + low + close) / 3)
- realised volatility over the day's one-minute log returns
- the minute with the most volume, the opening auction's 09:00 bar and the
  closing auction's 15:30 bar left out: both carry an auction's volume and
  would win every day
- thirteen half-hour buckets, 09:00 to 15:30, each with its return and its
  share of the day's volume; the 15:30 closing bar belongs to the last

The 09:00 bar holds the opening auction (on 2026-09-23, 632 thousand of
Samsung Electronics' 18.6 million shares), so the first bucket's volume share
is high by construction; the report says so rather than hide it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import time
from itertools import pairwise

ANALYSIS_VERSION = 1

OPEN_AUCTION = time(9, 0)
CLOSE_AUCTION = time(15, 30)
BUCKET_MINUTES = 30
# 09:00, 09:30, ... 15:00: thirteen, the last holding 15:00 to 15:30 inclusive.
BUCKETS = tuple(time(9 + (m // 60), m % 60) for m in range(0, 390, BUCKET_MINUTES))


@dataclass(frozen=True, slots=True)
class Bar:
    at: time
    """The minute's start in Seoul."""
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Bucket:
    start: time
    return_pct: float | None
    volume_share: float | None
    bars: int


@dataclass(frozen=True, slots=True)
class DaySummary:
    bars: int
    open: float
    close: float
    return_pct: float
    mfe_pct: float
    mae_pct: float
    high_at: time
    low_at: time
    minutes_to_high: int
    vwap: float | None
    close_vs_vwap_pct: float | None
    volatility_pct: float | None
    peak_volume_at: time | None
    volume: float
    first_hour_pct: float | None = None
    """From the day's open to the last close before 10:00."""
    buckets: list[Bucket] = field(default_factory=list)


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def bucket_of(t: time) -> time:
    index = min((_minutes(t) - 9 * 60) // BUCKET_MINUTES, len(BUCKETS) - 1)
    return BUCKETS[max(index, 0)]


def _pct(a: float, b: float) -> float:
    return (b / a - 1) * 100


def summarize(bars: Sequence[Bar]) -> DaySummary | None:
    """The day, or None when there is no bar or no price to measure from."""
    day = sorted(bars, key=lambda b: b.at)
    if not day or day[0].open <= 0:
        return None
    first, last = day[0], day[-1]
    high = max(day, key=lambda b: (b.high, -_minutes(b.at)))
    low = min(day, key=lambda b: (b.low, _minutes(b.at)))
    volume = sum(b.volume for b in day)

    vwap = (
        sum((b.high + b.low + b.close) / 3 * b.volume for b in day) / volume if volume > 0 else None
    )
    closes = [b.close for b in day if b.close > 0]
    returns = [math.log(b / a) for a, b in pairwise(closes)]
    volatility = math.sqrt(sum(r * r for r in returns)) * 100 if len(returns) >= 2 else None
    continuous = [b for b in day if b.at not in (OPEN_AUCTION, CLOSE_AUCTION)]
    peak = max(continuous, key=lambda b: (b.volume, -_minutes(b.at))) if continuous else None

    grouped: dict[time, list[Bar]] = {}
    for b in day:
        grouped.setdefault(bucket_of(b.at), []).append(b)
    buckets = []
    for start in BUCKETS:
        inside = grouped.get(start, [])
        buckets.append(
            Bucket(
                start=start,
                return_pct=_pct(inside[0].open, inside[-1].close)
                if inside and inside[0].open > 0
                else None,
                volume_share=sum(b.volume for b in inside) / volume if volume > 0 else None,
                bars=len(inside),
            )
        )

    first_hour = [b for b in day if b.at < time(10, 0)]
    return DaySummary(
        bars=len(day),
        open=first.open,
        close=last.close,
        return_pct=_pct(first.open, last.close),
        mfe_pct=_pct(first.open, high.high),
        mae_pct=_pct(first.open, low.low),
        high_at=high.at,
        low_at=low.at,
        minutes_to_high=_minutes(high.at) - _minutes(first.at),
        vwap=vwap,
        close_vs_vwap_pct=_pct(vwap, last.close) if vwap else None,
        volatility_pct=volatility,
        peak_volume_at=peak.at if peak else None,
        volume=volume,
        first_hour_pct=_pct(first.open, first_hour[-1].close) if first_hour else None,
        buckets=buckets,
    )


def index_day(bars: Sequence[Bar]) -> tuple[float | None, dict[time, float | None]]:
    """An index's open-to-close return and its return in each bucket, from its minutes."""
    day = sorted(bars, key=lambda b: b.at)
    if not day or day[0].open <= 0:
        return None, {}
    grouped: dict[time, list[Bar]] = {}
    for b in day:
        grouped.setdefault(bucket_of(b.at), []).append(b)
    per_bucket = {
        start: _pct(inside[0].open, inside[-1].close) if inside and inside[0].open > 0 else None
        for start, inside in grouped.items()
    }
    return _pct(day[0].open, day[-1].close), per_bucket
