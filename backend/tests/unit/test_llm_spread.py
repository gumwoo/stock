"""아침 LLM 예산을 종목마다 돌아가며 나누기(`spread_hits`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.repositories.news_repo import OpenHit
from app.services.llm_service import spread_hits

NOW = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)


def hit(item: int, stock: int, hours_ago: float) -> OpenHit:
    return OpenHit(item, stock, "q", "t", "s", None, 1, NOW - timedelta(hours=hours_ago))


def test_every_stock_gets_its_newest_before_any_gets_a_second() -> None:
    # 종목 1(흔한 낱말 이름)에 기사 다섯, 종목 2·3에 하나씩. 새것 순으로만 고르면 1이 셋을 다 먹는다.
    hits = [hit(i, 1, i) for i in range(1, 6)] + [hit(10, 2, 7), hit(11, 3, 8)]
    picked = spread_hits(hits, 3, now=NOW)
    assert [h.instrument_id for h in picked] == [1, 2, 3]
    assert picked[0].news_item_id == 1  # 종목 안에서는 가장 새 기사


def test_the_newest_news_stock_goes_first_in_a_round() -> None:
    hits = [hit(1, 1, 10), hit(2, 2, 1), hit(3, 3, 5)]
    assert [h.instrument_id for h in spread_hits(hits, 2, now=NOW)] == [2, 3]


def test_second_round_follows_and_limit_holds() -> None:
    hits = [hit(1, 1, 1), hit(2, 1, 2), hit(3, 2, 3), hit(4, 2, 4), hit(5, 1, 5)]
    assert [h.news_item_id for h in spread_hits(hits, 4, now=NOW)] == [1, 3, 2, 4]
    assert len(spread_hits(hits, 99, now=NOW)) == 5


def test_recent_articles_come_before_old_ones() -> None:
    # 오래된(72시간 넘은) 기사만 있는 종목은 최근 기사가 다 돈 뒤에 들어간다.
    hits = [hit(1, 1, 100), hit(2, 2, 1), hit(3, 2, 2)]
    assert [h.news_item_id for h in spread_hits(hits, 2, now=NOW)] == [2, 3]
    assert [h.news_item_id for h in spread_hits(hits, 3, now=NOW)] == [2, 3, 1]


def test_nothing_in_nothing_out() -> None:
    assert spread_hits([], 10, now=NOW) == []
    assert spread_hits([hit(1, 1, 1)], 0, now=NOW) == []
