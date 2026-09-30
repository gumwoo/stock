"""증권사 투자의견: 행 변환(원문·빈 목표가·빠진 증권사), 잘림 판정, 의견 정규화, 요약(창 경계·증권사별 최신·평균·상향/하향),
표시 격리, 일정."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.collectors.kis_invest_opinion import fetch_status, to_rows
from app.models.analyst import FETCH_OK, FETCH_TRUNCATED
from app.scoring.analyst import BUY, HOLD, OTHER, SELL, Report, normalize, summarize

# 2026-09-30 실측 응답의 한 행(삼성전자, 유안타).
YUANTA = {
    "stck_bsop_date": "20260923",
    "invt_opnn": "BUY",
    "invt_opnn_cls_code": "2",
    "rgbf_invt_opnn": "BUY",
    "rgbf_invt_opnn_cls_code": "3",
    "mbcr_name": "유안타",
    "hts_goal_prc": "630000",
    "stck_prdy_clpr": "276500",
}


class TestToRows:
    def test_a_real_row(self) -> None:
        rows, warnings = to_rows([YUANTA], 1)
        assert warnings == []
        r = rows[0]
        assert (r.report_date, r.broker, r.opinion, r.opinion_code) == (
            date(2026, 9, 23),
            "유안타",
            "BUY",
            "2",
        )
        assert r.target_price == Decimal("630000") and r.prev_close == Decimal("276500")

    def test_no_target_is_null(self) -> None:
        rows, _ = to_rows([{**YUANTA, "hts_goal_prc": "0", "invt_opnn": "Not Rated"}], 1)
        assert rows[0].target_price is None

    @pytest.mark.parametrize("field", ["mbcr_name", "stck_bsop_date", "invt_opnn"])
    def test_a_row_without_the_essentials_is_skipped(self, field: str) -> None:
        rows, warnings = to_rows([{**YUANTA, field: ""}, "rubbish"], 1)
        assert rows == [] and warnings == ["2 rows without a date, broker or opinion"]


def test_a_full_page_that_stops_inside_the_period_is_truncated() -> None:
    start = date(2025, 9, 30)
    assert fetch_status(100, date(2026, 1, 30), start) == FETCH_TRUNCATED  # SK하이닉스 1년 조회
    assert fetch_status(100, date(2025, 9, 30), start) == FETCH_OK
    assert fetch_status(4, date(2026, 8, 10), start) == FETCH_OK


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("BUY", BUY),
        ("매수", BUY),
        ("Strong BUY", BUY),
        ("Outperform", BUY),
        ("비중확대", BUY),
        ("중립", HOLD),
        ("Hold", HOLD),
        ("Market Perform", HOLD),
        ("매도", SELL),
        ("Underperform", SELL),
        ("Not Rated", OTHER),
    ],
)
def test_opinions_are_normalized(raw: str, label: str) -> None:
    assert normalize(raw) == label


def rep(i: int, d: date, broker: str, target: float | None, opinion: str = "매수") -> Report:
    return Report(i, d, broker, opinion, target)


class TestSummarize:
    DAY = date(2026, 9, 30)

    def test_only_reports_before_the_list_day_and_within_ninety_days(self) -> None:
        s = summarize(
            [
                rep(1, date(2026, 9, 30), "A", 999),  # 당일: 쓰지 않음
                rep(2, date(2026, 9, 29), "A", 200),
                rep(3, date(2026, 7, 2), "B", 100),  # 창 시작(9/30 - 90일 = 7/2) 포함
                rep(4, date(2026, 7, 1), "C", 50),  # 창 밖
            ],
            self.DAY,
            prev_close=100.0,
        )
        assert s["count"] == 2 and s["brokers"] == 2
        assert s["avg_target"] == 150 and s["upside_pct"] == 50.0
        assert s["latest"]["broker"] == "A" and s["latest"]["target"] == 200

    def test_each_broker_counts_once_with_its_latest(self) -> None:
        s = summarize(
            [rep(1, date(2026, 8, 1), "A", 100), rep(2, date(2026, 9, 1), "A", 120, "중립")],
            self.DAY,
            prev_close=None,
        )
        assert s["count"] == 2 and s["brokers"] == 1 and s["avg_target"] == 120
        assert s["upside_pct"] is None and s["opinions"][HOLD] == 1 and s["raised"] == 1

    def test_a_cut_compared_with_an_older_report(self) -> None:
        s = summarize(
            [rep(1, date(2026, 5, 1), "A", 150), rep(2, date(2026, 9, 1), "A", 120)],
            self.DAY,
            prev_close=None,
        )
        assert s["lowered"] == 1 and s["count"] == 1

    def test_nothing_in_the_window_is_zero_not_none(self) -> None:
        s = summarize([], self.DAY, prev_close=100.0)
        assert s["count"] == 0 and s["latest"] is None and s["avg_target"] is None

    def test_truncation_matters_only_inside_the_window(self) -> None:
        assert summarize([], self.DAY, prev_close=None, truncated_before=date(2026, 8, 1))[
            "truncated"
        ]
        assert not summarize([], self.DAY, prev_close=None, truncated_before=date(2026, 6, 1))[
            "truncated"
        ]


def test_a_failure_to_read_opinions_leaves_the_signal_tab_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    from app.api import lists

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(lists.analyst_service, "summaries", boom)
    session: Any = SimpleNamespace(begin_nested=nullcontext)
    assert lists._analysts(session, date(2026, 9, 30), [1], {}) == {}


def test_review_separates_names_with_and_without_reports() -> None:
    from app.services import list_review_service
    from app.services.list_review_service import Row

    def row(reports: int | None) -> Row:
        return Row("x", "WATCH", (), False, None, 0.0, 1.0, 0.0, 1.0, None, None, reports)

    g = {s.label for s in list_review_service.groups([row(3), row(0), row(None)])}
    assert "증권사 리포트 있음(90일)" in g and "증권사 리포트 없음(조회됨)" in g
