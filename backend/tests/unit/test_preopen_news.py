"""08:30 보충 수집기: 전체 스윕과 기록이 섞이지 않고, 받은 시각부터 풀 종목만 읽는다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.collectors.base import SkipCollection
from app.collectors.naver_news import NaverNewsCollector
from app.collectors.preopen_news import PreopenNewsSupplement
from app.core.calendar import Market
from app.models import Instrument

SWEPT = datetime(2026, 9, 28, 7, 0, 5, tzinfo=timezone(timedelta(hours=9)))


def _instrument(i: int) -> Instrument:
    inst = Instrument(market=Market.KR, name=f"종목{i}", tracked=False)
    inst.instrument_id = i
    return inst


def test_its_name_is_not_found_by_a_search_for_the_full_sweep() -> None:
    # 수집 기록은 이름 앞부분으로 찾는다(`LIKE 'NAVER_NEWS%'`). 보충의 SUCCESS가
    # 전체 스윕의 워터마크를 움직이면 안 된다.
    assert not PreopenNewsSupplement.name.startswith(NaverNewsCollector.name)


def test_it_reads_from_the_moment_it_is_given_not_three_days_back() -> None:
    c = PreopenNewsSupplement(instrument_ids=[1], since=SWEPT)
    assert c.watermark(None, now=SWEPT + timedelta(hours=1.5)) == SWEPT  # type: ignore[arg-type]
    assert c.since.tzinfo == UTC


def test_only_pool_names_are_asked_and_asking_them_all_is_complete() -> None:
    universe = [_instrument(i) for i in range(1, 6)]
    c = PreopenNewsSupplement(instrument_ids=[2, 4, 99], since=SWEPT)
    targets = c.targets(universe)
    assert [i.instrument_id for i in targets] == [2, 4]
    assert c.expected(universe, targets) == targets
    # 전체 스윕은 좁혀도 마스터 전체를 기준으로 센다.
    full = NaverNewsCollector(only=["종목2"])
    narrowed = full.targets(universe)
    assert [i.instrument_id for i in narrowed] == [2]
    assert full.expected(universe, narrowed) == universe


def test_an_empty_pool_is_skipped_not_reported_as_a_clean_sweep() -> None:
    c = PreopenNewsSupplement(instrument_ids=[], since=SWEPT)
    with pytest.raises(SkipCollection):
        c.collect(None)  # type: ignore[arg-type]
