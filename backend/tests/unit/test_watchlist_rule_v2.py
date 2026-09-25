"""PREOPEN_V2 선정 규칙: 그날 사건이 있는 종목만, 추적은 이유가 아니다."""

from __future__ import annotations

from app.scoring.watchlist import (
    DISCLOSURE_EVENT,
    DISCOVERY_SURGE,
    MAX_MEMBERS,
    NEGATIVE_NEWS_OVERLAY,
    POSITIVE_NEWS_OVERLAY,
    SEARCH_SURGE_REASON,
    SELECTION_VERSION_V2,
    STRATEGY_VERSION_V2,
    Seen,
    reasons_v2,
    select_names,
    select_names_v2,
)


def test_versions_are_their_own() -> None:
    assert (STRATEGY_VERSION_V2, SELECTION_VERSION_V2) == ("PREOPEN_V2", 2)


def test_tracking_and_a_high_score_are_not_reasons() -> None:
    quiet = Seen(1, tracked=True, last_action="BUY_INTEREST")
    assert reasons_v2(quiet) == ()
    assert select_names_v2([quiet]).picks == []
    # V1은 같은 종목을 넣는다. 바뀐 것이 바로 이 점이다.
    assert [p.instrument_id for p in select_names([quiet]).picks] == [1]


def test_a_tracked_name_with_news_is_in_for_the_news_only() -> None:
    seen = Seen(1, tracked=True, overlay_points=2.0, last_action="BUY_INTEREST")
    assert reasons_v2(seen) == (POSITIVE_NEWS_OVERLAY,)


def test_every_event_reason_counts_and_bad_news_is_one() -> None:
    seen = Seen(
        7,
        tracked=False,
        overlay_points=-1.0,
        has_disclosure_event=True,
        search_surge=2.0,
        discovery_score=3.3,
    )
    assert reasons_v2(seen) == (
        DISCOVERY_SURGE,
        NEGATIVE_NEWS_OVERLAY,
        DISCLOSURE_EVENT,
        SEARCH_SURGE_REASON,
    )


def test_just_below_every_threshold_is_nothing() -> None:
    seen = Seen(1, tracked=False, overlay_points=0.99, search_surge=1.99)
    assert reasons_v2(seen) == ()


def test_an_empty_morning_is_an_empty_list() -> None:
    selection = select_names_v2([Seen(i, tracked=bool(i % 2)) for i in range(1, 30)])
    assert (selection.picks, selection.left_out) == ([], 0)


def test_tracking_does_not_move_a_name_up() -> None:
    # 같은 이유, 같은 크기: 추적 여부가 아니라 종목 id가 동점을 가른다.
    tracked = Seen(9, True, overlay_points=1.5)
    untracked = Seen(3, False, overlay_points=1.5)
    assert [p.instrument_id for p in select_names_v2([tracked, untracked]).picks] == [3, 9]


def test_order_is_reasons_then_news_then_search_then_discovery() -> None:
    two = Seen(1, False, overlay_points=1.1, search_surge=2.1)
    big_news = Seen(2, False, overlay_points=-4.0)
    small_news = Seen(3, False, overlay_points=1.2)
    search = Seen(4, False, search_surge=5.0)
    found = Seen(5, False, discovery_score=9.0)
    order = [
        p.instrument_id for p in select_names_v2([found, search, small_news, big_news, two]).picks
    ]
    assert order == [1, 2, 3, 4, 5]


def test_forty_at_most_and_the_rest_counted() -> None:
    pool = [Seen(i, False, discovery_score=float(i)) for i in range(1, MAX_MEMBERS + 6)]
    selection = select_names_v2(pool)
    assert len(selection.picks) == MAX_MEMBERS
    assert selection.left_out == 5
    assert [p.rank for p in selection.picks] == list(range(1, MAX_MEMBERS + 1))


def test_among_disclosure_only_names_the_stronger_event_ranks_first() -> None:
    # 공시만 있는 종목끼리는 뉴스·검색·발굴 숫자가 모두 비어 있다. 종목 id보다
    # 공시 강도가 먼저 순서를 정한다.
    weak = Seen(1, False, has_disclosure_event=True, disclosure_intensity=0.3)
    strong = Seen(2, False, has_disclosure_event=True, disclosure_intensity=0.6)
    unknown = Seen(3, False, has_disclosure_event=True)
    order = [p.instrument_id for p in select_names_v2([weak, strong, unknown]).picks]
    assert order == [2, 1, 3]
