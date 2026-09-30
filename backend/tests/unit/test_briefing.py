"""아침 브리핑 카톡 문장: 순위 규칙(보류·재무 없음 제외, 동점은 목록 순위), 겹치는 종목 한 번, 모든 메시지 200자 이하,
증권사 줄(미조회 생략·0건 없음·잘림), 뉴스 없는 종목, 보내는 창."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.services.briefing_service import (
    MAX_CHARS,
    analyst_variants,
    build,
    news_messages,
    rankings,
)

DAY = date(2026, 9, 30)


def row(
    i: int,
    name: str,
    tech: float,
    fund: float | None,
    *,
    rank: int | None = None,
    action: str = "WATCH",
    analyst: Any = None,
) -> dict[str, Any]:
    missing = fund is None
    w = 0.6 if missing else 1.0
    total = tech * 0.6 + (0 if missing else fund * 0.4)  # type: ignore[operator]
    return {
        "instrument_id": i,
        "name": name,
        "code": f"{i:06d}",
        "rank": rank or i,
        "action": action,
        "total_score": total,
        "weight_total": w,
        "technical_score": tech,
        "fundamental_score": 0.0 if missing else fund,
        "list_reasons": ["DISCLOSURE_EVENT"],
        "heavyweight": False,
        "prev_limit": None,
        "analyst": analyst,
        "detail": {
            "factors": [
                {"engine": "TECHNICAL", "availability": "AVAILABLE", "effective_weight": 0.6},
                {
                    "engine": "FUNDAMENTAL",
                    "availability": "UNAVAILABLE" if missing else "AVAILABLE",
                    "effective_weight": 0.0 if missing else 0.4,
                },
            ]
        },
    }


def test_rankings_skip_abstained_and_missing_fundamentals() -> None:
    rows = [
        row(1, "가", 91.0, None),
        row(2, "나", 80.0, 90.0),
        row(3, "다", 70.0, 70.0),
        row(4, "라", 99.0, 99.0, action="ABSTAINED"),
        row(5, "마", 80.0, 60.0),
    ]
    r = rankings(rows)
    assert [x["name"] for x in r["기술"]] == ["가", "나", "마"]  # 동점(80)은 목록 순위
    assert [x["name"] for x in r["재무"]] == ["나", "다", "마"]  # 재무 없음(가)·보류(라) 제외
    assert [x["name"] for x in r["종합"]] == ["나", "마", "다"]  # 합계: 84, 72, 70 (가는 54.6)


def test_every_message_fits_and_a_name_appears_once() -> None:
    long_title = "아주 긴 제목 " * 30
    long_url = "https://n.news.naver.com/mnews/article/215/0001267315?sid=101"
    analyst = {
        "count": 19,
        "brokers": 14,
        "truncated": False,
        "avg_target": 3225000,
        "target_brokers": 14,
        "upside_pct": 82.7,
        "raised": 6,
        "lowered": 7,
        "latest": {
            "date": "2026-09-07",
            "broker": "미래에셋",
            "opinion": "매수",
            "label": "BUY",
            "target": 3100000,
        },
    }
    rows = [
        row(i, f"아주긴종목이름입니다{i}", 90.0 - i, 90.0 - i, analyst=analyst) for i in range(1, 8)
    ]
    events = {
        i: [{"event_type": "EARNINGS", "title": long_title, "url": long_url}] * 3
        for i in range(1, 8)
    }
    messages = build(DAY, rows, events)
    assert all(len(m) <= MAX_CHARS for m in messages)
    body = "\n".join(messages)
    assert body.count("■ 아주긴종목이름입니다1 ") == 1  # 기술1·재무1·종합1이어도 한 번


def test_analyst_line_distinguishes_not_fetched_from_none() -> None:
    assert analyst_variants(None) == []
    assert analyst_variants({"count": 0, "truncated": False}) == ["증권사(참고): 3개월 리포트 없음"]
    assert analyst_variants({"count": 0, "truncated": True}) == []


def test_a_name_without_news_says_so_and_titles_are_squeezed() -> None:
    messages = build(DAY, [row(1, "가", 90.0, 80.0)], {})
    assert "뉴스·공시 묶음 없음" in messages[-1]
    out = news_messages(
        "가", [{"title": "[공시] 기타시장안내              (주가 1,000원 미달)", "url": None}]
    )
    assert out == ["가 뉴스·공시\n1) [공시] 기타시장안내 (주가 1,000원 미달)"]


@pytest.mark.parametrize(
    ("now", "status"),
    [
        (datetime(2026, 9, 29, 23, 49, tzinfo=UTC), "NOT_YET"),  # 08:49 KST
        (datetime(2026, 9, 30, 0, 31, tzinfo=UTC), "LATE"),  # 09:31 KST
    ],
)
def test_outside_the_morning_window_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, now: datetime, status: str
) -> None:
    from app.api import briefing

    monkeypatch.setattr(briefing, "utc_now", lambda: now)
    out = briefing.briefing(None, day=None, force=False)  # type: ignore[arg-type]
    assert out["status"] == status and out["messages"] == []


def test_a_holiday_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import briefing

    monkeypatch.setattr(briefing, "utc_now", lambda: datetime(2026, 10, 5, 0, 0, tzinfo=UTC))
    assert briefing.briefing(None, day=None, force=False)["status"] == "HOLIDAY"  # type: ignore[arg-type]
