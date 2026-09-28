"""오늘의 관찰 종목 상세의 근거 뉴스·공시: 저장된 묶음을 화면용으로 바꾸고, 링크를 붙이되 실패가 목록을 막지 않는다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.realtime.gateway import (
    DART_VIEW,
    LiveEvent,
    LiveMember,
    _legacy_link,
    attach_links,
    event_link,
    news_link,
    to_events,
)

ASOF = datetime(2026, 9, 27, 23, 50, tzinfo=UTC)


def member(iid: int, *events: LiveEvent) -> LiveMember:
    return LiveMember(
        iid, f"{iid:06d}", "종목", 1, ("POSITIVE_NEWS_OVERLAY",), 3.6, None, None, events=events
    )


def event(title: str, **kw: object) -> LiveEvent:
    base: dict[str, object] = {
        "event_type": "ORDER_CONTRACT",
        "first_at": "2026-09-27T23:18:00+00:00",
        "title": title,
        "sentiment": 0.7,
        "articles": 1,
    }
    base.update(kw)
    return LiveEvent(**base)  # type: ignore[arg-type]


class TestToEvents:
    def test_a_stored_cluster_becomes_an_event_with_its_lead(self) -> None:
        raw = [
            {
                "event_type": "SHAREHOLDER_RETURN",
                "first_at": "2026-09-23T00:00:00+00:00",
                "articles": 1,
                "disclosures": 1,
                "sentiment": 0.6,
                "title": "[공시] 주요사항보고서(자기주식취득결정)",
                "lead": {"source": "DART", "id": 42},
            }
        ]
        (e,) = to_events(raw)
        assert (e.event_type, e.sentiment, e.articles, e.disclosures) == (
            "SHAREHOLDER_RETURN",
            0.6,
            1,
            1,
        )
        assert (e.lead_source, e.lead_id, e.url) == ("DART", 42, None)

    def test_old_rows_without_lead_and_broken_items_do_not_raise(self) -> None:
        # 2026-09-28 목록처럼 lead가 없는 행, 제목이 없는 항목, 숫자가 아닌 값이 섞여도 피드를 멈추지 않는다.
        raw = [
            {"title": "제목만 있는 옛 행"},
            {"event_type": "OTHER"},
            "not a dict",
            {"title": "숫자가 이상한 행", "articles": "many"},
        ]
        events = to_events(raw)
        assert [e.title for e in events] == ["제목만 있는 옛 행"]
        assert events[0].lead_source is None and events[0].event_type == "OTHER"
        assert to_events(None) == () and to_events({"title": "x"}) == ()


class TestLinks:
    def test_only_http_links_pass(self) -> None:
        assert event_link("https://n.news.naver.com/a/1") == "https://n.news.naver.com/a/1"
        assert event_link("HTTP://example.com") == "HTTP://example.com"
        assert event_link("javascript:alert(1)") is None
        assert event_link("") is None and event_link(None) is None

    def test_naver_mirror_first_then_the_original(self) -> None:
        assert (
            news_link("https://n.news.naver.com/a/1", "https://press.kr/1")
            == "https://n.news.naver.com/a/1"
        )
        assert news_link(None, "https://press.kr/1") == "https://press.kr/1"
        assert news_link("javascript:x", "https://press.kr/1") == "https://press.kr/1"

    def test_dart_view_url(self) -> None:
        assert DART_VIEW.startswith("https://dart.fss.or.kr/")


class TestAttachLinks:
    def test_links_are_attached_by_member_and_position(self) -> None:
        a = member(1, event("첫 기사"), event("둘째 기사"))
        b = member(2)
        out = attach_links([a, b], ASOF, lookup=lambda ms, asof: {(1, 1): "https://x/2"})
        assert [e.url for e in out[0].events] == [None, "https://x/2"]
        assert out[1] is b

    def test_a_failing_lookup_keeps_the_list_and_titles(self) -> None:
        # 링크 조회가 무슨 이유로든 실패해도(DB 오류 등) 목록과 제목은 그대로 나간다. 구독이 막히면 안 된다.
        def broken(ms: list[LiveMember], asof: datetime | None) -> dict[tuple[int, int], str]:
            raise RuntimeError("database is down")

        members = [member(1, event("첫 기사"))]
        out = attach_links(members, ASOF, lookup=broken)
        assert out == members and out[0].events[0].url is None

    def test_no_events_means_no_lookup(self) -> None:
        def never(ms: list[LiveMember], asof: datetime | None) -> dict[tuple[int, int], str]:
            raise AssertionError("should not be called")

        members = [member(1)]
        assert attach_links(members, ASOF, lookup=never) is members


def test_the_state_carries_the_events() -> None:
    from app.realtime.gateway import Gateway

    g = Gateway()
    g.members = [member(1, event("첫 기사", url="https://x/1"))]
    (m,) = g.state()["members"]
    assert m["events"] == [
        {
            "event_type": "ORDER_CONTRACT",
            "first_at": "2026-09-27T23:18:00+00:00",
            "title": "첫 기사",
            "sentiment": 0.7,
            "articles": 1,
            "disclosures": 0,
            "url": "https://x/1",
        }
    ]


@pytest.mark.parametrize(
    "bad", [[{"title": "t", "sentiment": "x"}], [{"title": "t", "lead": {"id": "x"}}]]
)
def test_unreadable_numbers_drop_only_that_event(bad: list[dict[str, object]]) -> None:
    assert to_events(bad) == ()


def test_a_naive_time_skips_only_that_event_before_any_query() -> None:
    # 시간대 없는 first_at은 asof(aware)와 비교할 수 없다. 예외로 전체 링크를 지우지 않고 그 이벤트만 링크 없이 둔다.
    # 조회 전에 돌아오므로 세션이 필요 없다.
    naive = event("시간대 없는 기사", first_at="2026-09-27T23:18:00")
    assert _legacy_link(None, 1, naive, ASOF, timedelta(hours=24)) is None
