"""Turning a Naver search page into rows, and refusing to overstate what it says.

No HTTP is mocked at the library level. The pure converters are called
directly and the transport is replaced by swapping `_get`, which is how the
DART collector is tested next door: a fake that returns the shape the API
returns is worth more than a fake that returns the shape a mocking library
finds convenient.

Two behaviours here are load-bearing rather than cosmetic.

Every request must be reserved first. `TestEveryCallIsReserved` counts both
sides, because a page fetched without a reservation is a call the ledger does
not know about, and an undercounted ledger is exactly how a cap gets exceeded.

A search hit is not a mention. Naver matches body text and related terms, so a
query returns articles that never name the company. `TestMentionsMustBeEarned`
pins that those are rejected rather than stored, since the cost of storing them
lands later, on a sentiment score attached to the wrong company.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.collectors.naver_news import PAGE_SIZE, NaverNewsCollector
from app.collectors.quota import QuotaExhausted
from app.core.quota import LimitSource, Quota
from app.models.news import MatchMethod, NewsSource

NOW = datetime(2026, 9, 22, 5, 0, tzinfo=UTC)
SINCE = NOW - timedelta(days=1)

QUOTA = Quota(
    key="probe",
    group="naver_search",
    official_limit=10,
    window=timedelta(hours=24),
    limit_source=LimitSource.OFFICIAL,
    note="test",
)


class FakeGuard:
    """Counts reservations, and can be told to run out."""

    def __init__(self, *, allow: int | None = None) -> None:
        self.reserved: list[tuple[str, str]] = []
        self._allow = allow

    def reserve(self, group: str, endpoint: str, *, calls: int = 1, now: Any = None) -> None:
        if self._allow is not None and len(self.reserved) >= self._allow:
            raise QuotaExhausted(
                quota=QUOTA, spent=self._allow, allowed=self._allow, retry_after=None
            )
        self.reserved.append((group, endpoint))


def item(
    *,
    title: str = "<b>삼성전자</b> 3분기 실적 발표",
    description: str = "삼성전자가 &quot;호실적&quot;을 기록했다",
    originallink: str = "https://news.example.com/article/1",
    link: str = "https://n.news.naver.com/mnews/article/1",
    pub: str = "Mon, 21 Sep 2026 14:03:00 +0900",
) -> dict[str, str]:
    return {
        "title": title,
        "description": description,
        "originallink": originallink,
        "link": link,
        "pubDate": pub,
    }


def collector(**kwargs: Any) -> NaverNewsCollector:
    kwargs.setdefault("guard", FakeGuard())
    return NaverNewsCollector(**kwargs)


class TestStripHtml:
    def test_highlighting_and_entities_are_removed(self) -> None:
        """Stored raw, this markup reaches a scoring prompt later."""
        assert (
            NaverNewsCollector.strip_html("<b>삼성전자</b> &quot;반도체&quot;")
            == '삼성전자 "반도체"'
        )

    def test_an_escaped_tag_in_the_article_survives_as_text(self) -> None:
        """Tags are dropped before entities are decoded. The other order turns
        text the article escaped on purpose into a tag and deletes it."""
        assert NaverNewsCollector.strip_html("&lt;b&gt; 태그 설명") == "<b> 태그 설명"

    def test_whitespace_is_collapsed(self) -> None:
        assert NaverNewsCollector.strip_html("  여러\n\n공백   ") == "여러 공백"


class TestParsePubDate:
    def test_an_offset_becomes_utc(self) -> None:
        parsed = NaverNewsCollector.parse_pub_date("Mon, 21 Sep 2026 14:03:00 +0900")

        assert parsed == datetime(2026, 9, 21, 5, 3, tzinfo=UTC)
        assert parsed is not None and parsed.tzinfo is not None

    @pytest.mark.parametrize(
        "raw", ["", "yesterday", "2026-09-21", "Mon, 32 Sep 2026 14:03:00 +0900"]
    )
    def test_an_unreadable_date_is_dropped_not_raised(self, raw: str) -> None:
        """One bad row costs that row. Raising would cost the sweep."""
        assert NaverNewsCollector.parse_pub_date(raw) is None

    def test_a_date_without_a_zone_is_refused(self) -> None:
        """Supplying a zone would be guessing, and a guess on an availability
        column is a silent nine-hour error rather than a visible failure."""
        assert NaverNewsCollector.parse_pub_date("Mon, 21 Sep 2026 14:03:00") is None


class TestCanonicalUrl:
    def test_the_publishers_link_wins_over_the_mirror(self) -> None:
        canonical = NaverNewsCollector.canonical_url(
            originallink="https://news.example.com/a", link="https://n.news.naver.com/b"
        )

        assert canonical == "https://news.example.com/a"

    def test_the_mirror_is_used_when_there_is_no_original(self) -> None:
        canonical = NaverNewsCollector.canonical_url(
            originallink="", link="https://n.news.naver.com/b"
        )

        assert canonical == "https://n.news.naver.com/b"

    def test_neither_link_means_no_article(self) -> None:
        assert NaverNewsCollector.canonical_url(originallink="", link="") is None
        assert NaverNewsCollector.canonical_url(originallink="not-a-url", link="") is None

    def test_query_order_and_fragments_do_not_make_two_articles(self) -> None:
        """The same piece reached two ways has to hash to one row, or the
        sentiment scorer pays for it twice and may disagree with itself."""
        first = NaverNewsCollector.canonical_url(
            originallink="https://News.Example.com/a?b=2&a=1#top", link=""
        )
        second = NaverNewsCollector.canonical_url(
            originallink="https://news.example.com/a?a=1&b=2", link=""
        )

        assert first == second
        assert NaverNewsCollector.url_hash(first or "") == NaverNewsCollector.url_hash(second or "")

    def test_tracking_parameters_are_dropped(self) -> None:
        canonical = NaverNewsCollector.canonical_url(
            originallink="https://news.example.com/a?utm_source=naver&id=7&fbclid=x", link=""
        )

        assert canonical == "https://news.example.com/a?id=7"

    def test_the_host_comes_along_for_free(self) -> None:
        assert NaverNewsCollector.publisher_host("https://news.example.com/a") == "news.example.com"


class TestToRows:
    def test_availability_is_publication(self) -> None:
        """News names a moment. The next-session rule exists for filings,
        whose dates name only a day."""
        rows, skipped, exhausted = NaverNewsCollector.to_rows([item()], since=None)

        ((row, _),) = rows
        assert row.available_at == row.published_at
        assert row.published_at == datetime(2026, 9, 21, 5, 3, tzinfo=UTC)
        assert (skipped, exhausted) == (0, False)

    def test_the_stored_text_carries_no_markup(self) -> None:
        rows, _, _ = NaverNewsCollector.to_rows([item()], since=None)

        ((row, _),) = rows
        assert "<b>" not in row.title
        assert row.title == "삼성전자 3분기 실적 발표"
        assert row.summary == '삼성전자가 "호실적"을 기록했다'
        assert row.source is NewsSource.NAVER_NEWS

    def test_the_mirror_is_kept_only_when_it_differs(self) -> None:
        same = "https://n.news.naver.com/x"
        rows, _, _ = NaverNewsCollector.to_rows([item(originallink="", link=same)], since=None)

        ((row, _),) = rows
        assert row.url == same
        assert row.naver_url is None

    def test_an_unusable_item_is_counted_not_dropped_silently(self) -> None:
        rows, skipped, _ = NaverNewsCollector.to_rows(
            [item(pub="nonsense"), item(originallink="", link=""), item(title="")],
            since=None,
        )

        assert rows == []
        assert skipped == 3

    def test_an_article_older_than_the_watermark_ends_the_sweep(self) -> None:
        """Results arrive newest first, so the first old one is the edge."""
        rows, _, exhausted = NaverNewsCollector.to_rows(
            [item(pub="Mon, 01 Sep 2026 14:03:00 +0900")], since=SINCE
        )

        assert rows == []
        assert exhausted is True


class TestMentionsMustBeEarned:
    def test_the_name_has_to_appear(self) -> None:
        method = NaverNewsCollector.match_method(
            "삼성전자 3분기 실적", name="삼성전자", aliases=(), symbol="005930"
        )

        assert method is MatchMethod.NAME

    def test_a_search_hit_that_never_names_the_company_is_rejected(self) -> None:
        """The reason this check exists. Naver matches related terms, so the
        query can return an article the company is not in — and a sentiment
        score attached to it would look entirely ordinary."""
        method = NaverNewsCollector.match_method(
            "반도체 업황이 회복되고 있다", name="삼성전자", aliases=(), symbol="005930"
        )

        assert method is None

    def test_spacing_does_not_make_it_a_different_company(self) -> None:
        """Korean coverage writes both spellings interchangeably."""
        method = NaverNewsCollector.match_method(
            "SK 하이닉스 실적", name="SK하이닉스", aliases=(), symbol=None
        )

        assert method is MatchMethod.NAME

    def test_an_alias_is_recorded_as_an_alias(self) -> None:
        """So a link made on a looser rule can be told apart afterwards."""
        method = NaverNewsCollector.match_method(
            "포스코 신사업", name="POSCO홀딩스", aliases=("포스코홀딩스", "포스코"), symbol=None
        )

        assert method is MatchMethod.ALIAS

    def test_the_ticker_counts_but_is_the_last_resort(self) -> None:
        method = NaverNewsCollector.match_method(
            "005930 종목 분석", name="삼성전자", aliases=(), symbol="005930"
        )

        assert method is MatchMethod.SYMBOL


class TestEveryCallIsReserved:
    def _paging(self, collector_: NaverNewsCollector, pages: list[list[dict[str, str]]]) -> None:
        calls: list[int] = []

        def fake_get(client: Any, *, query: str, start: int) -> dict[str, Any]:
            calls.append(start)
            index = (start - 1) // PAGE_SIZE
            return {"items": pages[index] if index < len(pages) else []}

        collector_._get = fake_get  # type: ignore[assignment,method-assign]
        collector_.calls = calls  # type: ignore[attr-defined]

    def test_a_reservation_precedes_every_page(self) -> None:
        """Counted on both sides. A page fetched without one is a call the
        ledger never sees, which is how a budget is quietly exceeded.

        The fake goes in underneath `_get`, at the transport, so the real
        reservation is the one being counted. Replacing `_get` itself, as this
        once did, left the stub reserving on its own behalf — both sides of
        "counted on both sides" were then the test, and deleting the
        reservation from the collector changed nothing here.
        """
        guard = FakeGuard()
        c = NaverNewsCollector(guard=guard, max_pages=3)
        full = [item(originallink=f"https://news.example.com/{i}") for i in range(PAGE_SIZE)]
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params["start"])
            return httpx.Response(200, json={"items": full if len(seen) < 2 else full[:5]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            c._sweep(client, query="삼성전자", since=None)  # type: ignore[arg-type]

        assert len(seen) == len(guard.reserved) == 2

    def test_paging_stops_on_a_short_page(self) -> None:
        c = collector(max_pages=5)
        self._paging(
            c, [[item(originallink=f"https://e.com/{i}") for i in range(PAGE_SIZE)], [item()]]
        )

        sweep = c._sweep(None, query="q", since=None)  # type: ignore[arg-type]

        assert c.calls == [1, 101]  # type: ignore[attr-defined]
        assert sweep.hit_page_cap is False

    def test_paging_stops_at_the_page_cap_and_says_so(self) -> None:
        """A deliberate truncation, reported in the detail rather than as
        partial collection — otherwise the busiest name makes every run
        PARTIAL and PARTIAL stops meaning anything."""
        c = collector(max_pages=2)
        full = [item(originallink=f"https://e.com/{i}") for i in range(PAGE_SIZE)]
        self._paging(c, [full, full, full])

        sweep = c._sweep(None, query="q", since=None)  # type: ignore[arg-type]

        assert c.calls == [1, 101]  # type: ignore[attr-defined]
        assert sweep.hit_page_cap is True

    def test_an_exhausted_budget_stops_the_sweep(self) -> None:
        """The guard is the floor. Reaching it is the mechanism working."""
        guard = FakeGuard(allow=1)
        c = NaverNewsCollector(guard=guard, max_pages=5)
        full = [item(originallink=f"https://e.com/{i}") for i in range(PAGE_SIZE)]
        seen: list[int] = []

        def guarded_get(client: Any, *, query: str, start: int) -> dict[str, Any]:
            c._guard.reserve("naver_search", "news")
            seen.append(start)
            return {"items": full}

        c._get = guarded_get  # type: ignore[assignment,method-assign]

        sweep = c._sweep(None, query="q", since=None)  # type: ignore[arg-type]

        assert len(seen) == 1
        assert isinstance(sweep.exhausted, QuotaExhausted)

    def test_the_page_the_budget_already_paid_for_is_kept(self) -> None:
        """Refusal on page two must not discard page one.

        That page was fetched and charged against the same budget. Throwing it
        away at the moment the budget runs out wastes the last thing it bought,
        and the run would report having collected nothing from this name.
        """
        guard = FakeGuard(allow=1)
        c = NaverNewsCollector(guard=guard, max_pages=5)
        full = [item(originallink=f"https://e.com/{i}") for i in range(PAGE_SIZE)]

        def guarded_get(client: Any, *, query: str, start: int) -> dict[str, Any]:
            c._guard.reserve("naver_search", "news")
            return {"items": full}

        c._get = guarded_get  # type: ignore[assignment,method-assign]

        sweep = c._sweep(None, query="q", since=None)  # type: ignore[arg-type]

        assert len(sweep.rows) == PAGE_SIZE
        assert sweep.read == PAGE_SIZE
        assert sweep.exhausted is not None


class TestConfiguration:
    def test_missing_credentials_name_the_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = collector()
        monkeypatch.setattr(c, "_client_id", "")

        assert c.is_configured() is False
        assert "NAVER_CLIENT_ID" in c.skip_reason()
        assert "NAVER_CLIENT_SECRET" in c.skip_reason()

    def test_an_ambiguous_name_can_be_given_a_better_query(self) -> None:
        """`NAVER` as a search term returns the portal, not the company."""
        from app.models import Instrument

        naver = Instrument(name="NAVER", market="KR")
        samsung = Instrument(name="삼성전자", market="KR")

        assert NaverNewsCollector.query_for(naver) == "네이버 주가"
        assert NaverNewsCollector.query_for(samsung) == "삼성전자"
        assert NaverNewsCollector.aliases_for(naver) == ("네이버",)


class TestALongerNameWins:
    """Short company names sit inside longer ones, and 252 of them do.

    Measured on the real `corpCode.xml`: 252 of 3,961 listed names are a
    substring of another listed name. `한화` is inside `대한화섬` and `LG` is
    inside thirteen others, and the relation is not a prefix, so nothing about
    the surrounding characters can be used to tell them apart. Getting this
    wrong sends a company's articles to a different company, and the sentiment
    score that lands on them looks entirely ordinary.
    """

    def test_a_name_inside_another_company_is_not_a_mention(self) -> None:
        assert (
            NaverNewsCollector.match_method(
                "대한화섬 3분기 실적 발표", name="한화", conflicts=("대한화섬",)
            )
            is None
        )

    def test_a_sibling_listing_does_not_speak_for_the_parent(self) -> None:
        assert (
            NaverNewsCollector.match_method(
                "한화오션 대형 수주", name="한화", conflicts=("한화오션",)
            )
            is None
        )

    def test_the_company_itself_still_matches_through_a_particle(self) -> None:
        """Korean attaches particles straight to the noun.

        A boundary rule that demanded a non-Hangul character after the name
        would reject `한화는`, which is simply the subject of a sentence, so
        containment is the only rule that can work here.
        """
        assert (
            NaverNewsCollector.match_method(
                "한화는 3분기 실적을 발표했다", name="한화", conflicts=("대한화섬", "한화오션")
            )
            is MatchMethod.NAME
        )

    def test_the_conflict_set_comes_from_the_master(self) -> None:
        registry = NaverNewsCollector.registry(["한화", "한화오션", "대한화섬", "삼성전자"])

        assert set(NaverNewsCollector.conflicts_for(("한화",), registry)) == {
            "한화오션",
            "대한화섬",
        }

    def test_a_name_is_not_its_own_conflict(self) -> None:
        registry = NaverNewsCollector.registry(["한화", "삼성전자"])

        assert NaverNewsCollector.conflicts_for(("한화",), registry) == ()

    def test_an_unrelated_article_is_still_rejected(self) -> None:
        assert (
            NaverNewsCollector.match_method(
                "반도체 업황 회복 조짐", name="한화", conflicts=("대한화섬",)
            )
            is None
        )


class TestHowANameIsWritten:
    def test_a_spaced_spelling_counts(self) -> None:
        assert (
            NaverNewsCollector.match_method("SK 하이닉스 신고가", name="SK하이닉스")
            is MatchMethod.NAME
        )

    def test_a_lowercase_spelling_counts(self) -> None:
        """Copy writes `naver`; the register says `NAVER`."""
        assert NaverNewsCollector.match_method("naver 3분기 실적", name="NAVER") is MatchMethod.NAME

    def test_an_alias_is_reported_as_an_alias(self) -> None:
        assert (
            NaverNewsCollector.match_method("네이버 주가 상승", name="NAVER", aliases=("네이버",))
            is MatchMethod.ALIAS
        )

    def test_a_code_with_a_digit_beside_it_is_a_different_number(self) -> None:
        """`종가1005930원` contains 005930 and means nothing of the sort."""
        assert (
            NaverNewsCollector.match_method("종가1005930원", name="삼성전자", symbol="005930")
            is None
        )

    def test_a_code_standing_alone_counts(self) -> None:
        assert (
            NaverNewsCollector.match_method("코드 005930 상승", name="삼성전자", symbol="005930")
            is MatchMethod.SYMBOL
        )


class TestOneSpellingPerArticle:
    @staticmethod
    def canon(url: str) -> str | None:
        return NaverNewsCollector.canonical_url(originallink=url, link="")

    def test_a_trailing_slash_is_the_same_article(self) -> None:
        assert self.canon("https://e.com/a/") == self.canon("https://e.com/a")

    def test_a_default_port_is_the_same_host(self) -> None:
        assert self.canon("https://e.com:443/a") == self.canon("https://e.com/a")

    def test_an_ordinary_parameter_keeps_two_articles_apart(self) -> None:
        """`ref` and `from` are English words before they are trackers.

        Stripping one that a publisher uses as an article id merges two
        different pieces into one row, and the survivor inherits the other's
        mentions — a mention on an article that never named the company. A
        duplicate row, the cost of not stripping, is harmless by comparison.
        """
        assert self.canon("https://e.com/n?ref=1") != self.canon("https://e.com/n?ref=2")
        assert self.canon("https://e.com/n?from=2") != self.canon("https://e.com/n?from=3")

    def test_a_real_tracker_still_folds(self) -> None:
        assert self.canon("https://e.com/a?utm_source=x") == self.canon("https://e.com/a")
