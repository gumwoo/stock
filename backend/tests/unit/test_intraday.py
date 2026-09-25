"""A session summarised from its minutes: the numbers, the auctions, the half-hours."""

from __future__ import annotations

import math
from datetime import time
from itertools import pairwise

import pytest

from app.scoring.intraday import BUCKETS, Bar, bucket_of, index_day, summarize


def bar(hhmm: str, o: float, h: float, lo: float, c: float, v: float = 100.0) -> Bar:
    return Bar(time(int(hhmm[:2]), int(hhmm[2:])), o, h, lo, c, v)


DAY = [
    bar("0900", 100, 101, 99, 100, 5000),  # the opening auction's bar
    bar("0901", 100, 103, 100, 102, 300),
    bar("0930", 102, 104, 101, 103, 200),
    bar("1200", 103, 103, 95, 96, 900),
    bar("1519", 96, 98, 96, 97, 400),
    bar("1530", 98, 98, 98, 98, 6000),  # the closing auction's bar
]


class TestTheDay:
    def test_open_to_close_and_the_room_either_way(self) -> None:
        s = summarize(DAY)
        assert s is not None
        assert (s.open, s.close) == (100, 98)
        assert s.return_pct == pytest.approx(-2.0)
        assert s.mfe_pct == pytest.approx(4.0)  # 104 at 09:30
        assert s.mae_pct == pytest.approx(-5.0)  # 95 at 12:00
        assert (s.high_at, s.low_at) == (time(9, 30), time(12, 0))
        assert s.minutes_to_high == 30

    def test_the_auctions_do_not_win_the_busiest_minute(self) -> None:
        s = summarize(DAY)
        assert s is not None and s.peak_volume_at == time(12, 0)

    def test_vwap_is_weighted_by_volume_at_the_typical_price(self) -> None:
        s = summarize(DAY)
        assert s is not None
        total = sum(b.volume for b in DAY)
        expected = sum((b.high + b.low + b.close) / 3 * b.volume for b in DAY) / total
        assert s.vwap == pytest.approx(expected)
        assert s.close_vs_vwap_pct == pytest.approx((98 / expected - 1) * 100)

    def test_volatility_is_the_root_sum_of_squared_minute_returns(self) -> None:
        s = summarize(DAY)
        closes = [b.close for b in DAY]
        rs = [math.log(b / a) for a, b in pairwise(closes)]
        assert s is not None and s.volatility_pct == pytest.approx(
            math.sqrt(sum(r * r for r in rs)) * 100
        )

    def test_order_of_arrival_does_not_matter(self) -> None:
        assert summarize(DAY) == summarize(list(reversed(DAY)))

    def test_no_bars_or_no_opening_price_is_nothing_to_measure(self) -> None:
        assert summarize([]) is None
        assert summarize([bar("0900", 0, 1, 0, 1)]) is None


class TestHalfHours:
    def test_thirteen_buckets_and_the_closing_bar_in_the_last(self) -> None:
        assert len(BUCKETS) == 13
        assert bucket_of(time(9, 0)) == time(9, 0)
        assert bucket_of(time(9, 29)) == time(9, 0)
        assert bucket_of(time(9, 30)) == time(9, 30)
        assert bucket_of(time(15, 19)) == time(15, 0)
        assert bucket_of(time(15, 30)) == time(15, 0)

    def test_each_bucket_has_its_return_and_its_share_of_volume(self) -> None:
        s = summarize(DAY)
        assert s is not None
        by = {b.start: b for b in s.buckets}
        first = by[time(9, 0)]
        assert first.bars == 2
        assert first.return_pct == pytest.approx(2.0)  # 100 open to 102 close
        assert first.volume_share == pytest.approx(5300 / 12800)
        empty = by[time(10, 0)]
        assert (empty.bars, empty.return_pct, empty.volume_share) == (0, None, 0.0)
        assert sum(b.volume_share or 0 for b in s.buckets) == pytest.approx(1.0)


def test_an_index_day_open_to_close_and_by_bucket() -> None:
    whole, buckets = index_day(
        [
            bar("0900", 100, 100, 100, 100),
            bar("0931", 101, 101, 101, 102),
            bar("1530", 103, 103, 103, 103),
        ]
    )
    assert whole == pytest.approx(3.0)
    assert buckets[time(9, 0)] == pytest.approx(0.0)
    assert buckets[time(9, 30)] == pytest.approx((102 / 101 - 1) * 100)


def test_the_first_hour_is_from_the_open_to_the_last_close_before_ten() -> None:
    s = summarize(DAY)
    assert s is not None and s.first_hour_pct == pytest.approx(3.0)  # 100 open, 103 at 09:30
