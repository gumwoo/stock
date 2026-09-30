"""list-review 추가분: 시가→9:30 최고(첫 봉 시가 기준, 09:30 봉 제외, 잠김 표시), 점수 TOP3 묶음(대형주 포함), 새 열, 인자 검사."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services import list_review_service
from app.services.list_review_service import Row, peak_930, stats


def bar(
    hh: int, mm: int, o: float, h: float, lo: float
) -> tuple[datetime, Decimal, Decimal, Decimal]:
    return (datetime(2026, 9, 29, hh - 9, mm, tzinfo=UTC), Decimal(o), Decimal(h), Decimal(lo))


def test_peak_is_from_the_first_open_to_the_highest_high_before_nine_thirty() -> None:
    bars = [
        bar(9, 0, 20350, 20950, 19460),
        bar(9, 20, 21400, 21500, 21300),
        bar(9, 30, 21100, 21600, 21000),
    ]
    peak, locked = peak_930(bars)
    assert peak == pytest.approx((21500 / 20350 - 1) * 100)  # 09:30 봉(21600)은 빼고
    assert not locked


def test_a_book_locked_at_one_price_is_marked() -> None:
    bars = [bar(9, 2, 12870, 12870, 12870), bar(9, 3, 12870, 12870, 12870)]
    assert peak_930(bars) == (0.0, True)


def test_no_bar_before_nine_thirty_is_none() -> None:
    assert peak_930([bar(9, 31, 100, 110, 90)]) == (None, False)
    assert peak_930([]) == (None, False)


def row(name: str, tops: tuple[str, ...], peak: float | None, *, heavy: bool = False) -> Row:
    return Row(name, "WATCH", (), heavy, None, 0.0, 1.0, 0.0, 1.0, peak_930=peak, tops=tops)


def test_top3_groups_keep_heavyweights_and_count_two_percent() -> None:
    rows = [
        row("에프앤가이드", ("재무1", "종합1"), 5.65),
        row("SK하이닉스", ("재무2",), 1.54, heavy=True),
        row("칩스앤미디어", ("기술1",), 0.66),
        row("기타", (), 3.0),
    ]
    g = {s.label: s for s in list_review_service.groups(rows)}
    fund = g["재무 TOP3(현재 규칙, 대형주 포함)"]
    assert (
        fund.n == 2
        and fund.peak_930 == pytest.approx((5.65 + 1.54) / 2)
        and fund.peak_930_2pct == 1
    )
    assert (
        g["기술 TOP3(현재 규칙, 대형주 포함)"].n == 1
        and g["종합 TOP3(현재 규칙, 대형주 포함)"].n == 1
    )


def test_stats_ignore_names_without_a_peak() -> None:
    s = stats("x", [row("a", (), None), row("b", (), 2.0)])
    assert s.peak_930 == 2.0 and s.peak_930_2pct == 1


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [(None, None, None), ("2026-09-29", "2026-09-28", "2026-09-30"), (None, "2026-09-28", None)],
)
def test_the_command_wants_either_a_day_or_a_range(
    day: str | None, start: str | None, end: str | None
) -> None:
    from datetime import date

    from app.cli import cmd_list_review

    def d(x: str | None) -> date | None:
        return date.fromisoformat(x) if x else None

    assert cmd_list_review(d(day), d(start), d(end)) == 2
