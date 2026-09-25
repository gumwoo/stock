"""Reading KIS minute bars: the minute a label names, walking a day back, and the index pieces.

The scripted pages copy what KIS returned for Samsung Electronics on
2026-09-23: 120 bars a page ending at the cursor (the cursor bar included),
nothing between 15:20 and 15:29, and past 09:00 the previous day's
after-hours bars.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.collectors.base import UpstreamUnavailableError
from app.collectors.kis_minute import (
    MAX_PAGES,
    DayWalk,
    KisMinuteCollector,
    index_rows,
    minute_start,
    stock_bar,
)
from app.repositories.minute_repo import COMPLETE, EMPTY, PARTIAL

DAY = date(2026, 9, 23)


def labels_of_day(thin: bool = False) -> list[str]:
    """Newest first: 15:30, then 15:19 back to 09:00 (or every seventh minute if thin)."""
    minutes = [15 * 60 + 19 - i for i in range(380)]
    if thin:
        minutes = minutes[::7]
    return ["153000"] + [f"{m // 60:02d}{m % 60:02d}00" for m in minutes]


def row(day: str, label: str, price: str = "1000") -> dict[str, str]:
    return {
        "stck_bsop_date": day,
        "stck_cntg_hour": label,
        "stck_prpr": price,
        "stck_oprc": price,
        "stck_hgpr": price,
        "stck_lwpr": price,
        "cntg_vol": "10",
        "acml_tr_pbmn": "0",
    }


class Provider:
    """Pages as KIS serves them: up to 120 bars at or before the cursor, then the day before."""

    def __init__(self, labels: list[str], *, day: str = "20260923") -> None:
        after_hours = [f"{h:02d}{m:02d}00" for h in range(19, 15, -1) for m in range(59, -1, -1)]
        self.stream = [row(day, lab) for lab in labels] + [
            row("20260922", lab) for lab in after_hours
        ]
        self.asked: list[str] = []

    def page(self, cursor: str) -> list[dict[str, str]]:
        self.asked.append(cursor)
        start = next(
            i
            for i, r in enumerate(self.stream)
            if r["stck_bsop_date"] < "20260923" or r["stck_cntg_hour"] <= cursor
        )
        return self.stream[start : start + 120]


def walk(provider: Provider) -> DayWalk:
    w = DayWalk(DAY)
    while w.status is None:
        w.take(provider.page(w.cursor))
    return w


class TestLabels:
    def test_a_label_is_the_start_of_that_minute_in_seoul(self) -> None:
        assert minute_start(DAY, "090000") == datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
        assert minute_start(DAY, "153000") == datetime(2026, 9, 23, 6, 30, tzinfo=UTC)

    def test_a_bar_is_available_when_its_minute_is_over(self) -> None:
        bar = stock_bar(7, DAY, row("20260923", "091500", "284000"))
        assert bar.available_at - bar.ts == timedelta(minutes=1)
        assert (bar.close, bar.volume) == (Decimal("284000"), Decimal("10"))

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("stck_cntg_hour", "9150"),
            ("stck_cntg_hour", "0915xx"),
            ("stck_prpr", "abc"),
            ("cntg_vol", "-1"),
            ("stck_prpr", "0"),
        ],
    )
    def test_a_malformed_bar_is_an_upstream_error(self, key: str, value: str) -> None:
        bad = row("20260923", "091500") | {key: value}
        with pytest.raises(UpstreamUnavailableError):
            stock_bar(7, DAY, bad)


class TestWalkingADay:
    def test_a_full_day_is_381_bars_in_four_pages(self) -> None:
        provider = Provider(labels_of_day())
        w = walk(provider)
        assert w.status == COMPLETE
        assert len(w.rows) == 381
        assert min(w.rows) == "090000" and max(w.rows) == "153000"
        assert "152500" not in w.rows
        assert w.pages == 4
        # Each next page starts at the oldest bar seen, which comes back again.
        assert provider.asked[0] == "153000" and len(set(provider.asked)) == 4

    def test_a_thin_day_ends_when_the_day_before_appears(self) -> None:
        w = walk(Provider(labels_of_day(thin=True)))
        assert w.status == COMPLETE
        assert all(k.startswith(("09", "1")) for k in w.rows)
        assert len(w.rows) == len(labels_of_day(thin=True))

    def test_the_day_before_is_not_kept(self) -> None:
        w = walk(Provider(labels_of_day(thin=True)))
        assert all(r["stck_bsop_date"] == "20260923" for r in w.rows.values())

    def test_a_day_with_no_bars_is_empty(self) -> None:
        w = DayWalk(DAY)
        w.take([row("20260922", "155900")])
        assert (w.status, w.rows) == (EMPTY, {})

    def test_a_cursor_that_does_not_move_stops_the_walk_as_partial(self) -> None:
        w = DayWalk(DAY)
        stuck = [row("20260923", lab) for lab in labels_of_day()[:120]]
        w.take(stuck)
        w.take(stuck)
        assert w.status == PARTIAL

    def test_the_page_cap_stops_the_walk_as_partial(self) -> None:
        # A provider that hands back one bar a page would never reach 09:00.
        labels = labels_of_day()
        w = DayWalk(DAY)
        for n in range(MAX_PAGES):
            if w.status is not None:
                break
            w.take([row("20260923", labels[n])])
        assert w.status == PARTIAL
        assert w.pages == MAX_PAGES

    def test_a_row_that_is_not_an_object_is_an_upstream_error(self) -> None:
        with pytest.raises(UpstreamUnavailableError):
            DayWalk(DAY).take(["20260923"])


def index_row(day: str, label: str, price: str = "7000") -> dict[str, str]:
    return {
        "stck_bsop_date": day,
        "stck_cntg_hour": label,
        "bstp_nmix_prpr": price,
        "bstp_nmix_oprc": price,
        "bstp_nmix_hgpr": price,
        "bstp_nmix_lwpr": price,
        "cntg_vol": "1",
        "acml_tr_pbmn": "1",
    }


class TestIndexPieces:
    def test_summary_lines_other_days_and_after_the_close_are_dropped(self) -> None:
        page = [
            index_row("20260923", "999999", "7080.92"),
            index_row("20260923", "888888"),
            index_row("20260923", "153200"),
            index_row("20260923", "153000", "7078.93"),
            index_row("20260923", "135300"),
            index_row("20260922", "150000"),
        ]
        rows = index_rows("^KS11", DAY, page)
        assert [r.ts for r in rows] == [minute_start(DAY, "153000"), minute_start(DAY, "135300")]
        assert rows[0].close == Decimal("7078.93")


class TestWhichDays:
    def collector(self, backfill: int) -> KisMinuteCollector:
        return KisMinuteCollector(instrument_ids=[1], backfill_sessions=backfill)

    def test_today_after_the_close_then_the_sessions_before_newest_first(self) -> None:
        after_close = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)  # 17:00 in Seoul
        days = self.collector(3).days(after_close)
        assert days == [date(2026, 9, 23), date(2026, 9, 22), date(2026, 9, 21), date(2026, 9, 18)]

    def test_not_today_while_the_session_is_running(self) -> None:
        during = datetime(2026, 9, 23, 3, 0, tzinfo=UTC)  # 12:00 in Seoul
        assert self.collector(1).days(during) == [date(2026, 9, 22)]

    def test_a_holiday_has_no_today(self) -> None:
        holiday = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
        assert self.collector(1).days(holiday) == [date(2026, 9, 23)]


def test_a_bar_outside_the_regular_session_is_not_kept() -> None:
    """Pre-market or after-close bars of the same day, should the provider send any."""
    w = DayWalk(DAY)
    w.take([row("20260923", "153100"), row("20260923", "090000"), row("20260923", "085900")])
    assert set(w.rows) == {"090000"}


def test_an_answer_with_nothing_at_all_is_asked_again_not_settled() -> None:
    w = DayWalk(DAY)
    w.take([])
    assert w.status == PARTIAL


def test_an_empty_page_after_some_bars_is_not_the_end_of_the_day() -> None:
    """Only the day before appearing proves the day is over."""
    w = DayWalk(DAY)
    w.take([row("20260923", lab) for lab in labels_of_day()[:120]])
    w.take([])
    assert w.status == PARTIAL
