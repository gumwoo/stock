"""테마어 뉴스: 아침이 속한 거래일, 창의 시작, 기사 속 종목 이름 세기."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.collectors import theme_news as t
from app.collectors.naver_news import NaverNewsCollector


def kst(y: int, m: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=ZoneInfo("Asia/Seoul")).astimezone(UTC)


def test_morning_day_is_today_before_the_close_and_the_next_session_after() -> None:
    assert t.morning_day(kst(2026, 9, 22, 7)) == date(2026, 9, 22)  # 화 07:00
    assert t.morning_day(kst(2026, 9, 22, 16)) == date(2026, 9, 23)  # 화 장 마감 뒤
    # 추석 연휴(9/24~9/25)와 주말을 건너 다음 거래일은 9/28(월)
    assert t.morning_day(kst(2026, 9, 26, 14)) == date(2026, 9, 28)


def test_window_starts_at_the_previous_session_close() -> None:
    assert t.window_start(date(2026, 9, 28)) == kst(2026, 9, 23, 15, 30)
    assert t.window_start(date(2026, 9, 22)) == kst(2026, 9, 21, 15, 30)


def test_mentions_count_each_confirmed_name_once_per_article() -> None:
    registry = NaverNewsCollector.registry(["SK하이닉스", "한미반도체", "삼성전자"])
    articles = [
        ("SK하이닉스, HBM 공급 확대", "SK 하이닉스 주가가 올랐다. SK하이닉스는"),
        ("AI 반도체 랠리", "한미반도체와 SK하이닉스 강세"),
        ("엔비디아 실적", "미국 증시 마감"),
    ]
    got = t.mentions(articles, registry)
    assert got == {"SK하이닉스": 2, "한미반도체": 1}


def test_the_theme_sweep_never_looks_like_the_full_sweep() -> None:
    # 수집 기록은 이름 앞부분으로 찾는다. NAVER_NEWS로 시작하면 전체 스윕의 워터마크를 움직인다.
    assert not t.ThemeNewsCollector.name.startswith("NAVER_NEWS")
    assert t.ThemeNewsCollector.endpoint != NaverNewsCollector.endpoint
    assert len({th.key for th in t.THEMES}) == len(t.THEMES)


def test_generic_word_names_and_weak_only_matches_are_not_counted() -> None:
    registry = NaverNewsCollector.registry(["디바이스", "대상", "삼성전자"])
    articles = [("반도체 주가 급등", "모바일 디바이스 수요, 대상 기업 증시 강세. 삼성전자 상승")]
    assert t.mentions(articles, registry) == {"삼성전자": 1}
