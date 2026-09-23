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

from app.collectors.base import UpstreamUnavailableError
from app.collectors.naver_news import PAGE_SIZE, REPORT_TOP, NaverNewsCollector, Yield
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


class TestLatinNamesHaveWordBoundaries:
    """`KT` is inside `KTX`, `SK` inside `TASK`, `LG` inside `ALGO`.

    The longest-registered-name rule cannot help here: the word swallowing the
    name is not a company, so there is nothing in the master to compare
    against. Latin script does have boundaries, though, and the companies with
    two-letter Latin names are among the most written about in the market.
    """

    def test_a_latin_name_inside_an_ordinary_word_is_rejected(self) -> None:
        for text, name in (
            ("KTX 특송 화물 증가", "KT"),
            ("TASK FORCE 가동", "SK"),
            ("ALGO 트레이딩 확대", "LG"),
            ("CJK 인코딩 오류", "CJ"),
        ):
            assert NaverNewsCollector.match_method(text, name=name) is None, (text, name)

    def test_the_company_still_matches_before_a_particle(self) -> None:
        """A Korean particle is not ASCII, so it is not a word character here."""
        assert (
            NaverNewsCollector.match_method("KT는 요금제를 개편했다", name="KT") is MatchMethod.NAME
        )
        assert NaverNewsCollector.match_method("SK 실적 발표", name="SK") is MatchMethod.NAME

    def test_a_name_with_punctuation_is_left_alone(self) -> None:
        """`KT&G` is distinctive enough that the rule would only cost matches."""
        assert NaverNewsCollector.match_method("KT&G 담배 매출", name="KT&G") is MatchMethod.NAME

    def test_a_hangul_name_is_left_alone(self) -> None:
        assert (
            NaverNewsCollector.match_method("삼성전자는 반도체", name="삼성전자")
            is MatchMethod.NAME
        )

    def test_conflicts_and_matching_fold_case_the_same_way(self) -> None:
        """`spans` ignores case, so `conflicts_for` must too.

        Otherwise a registry spelling that differs only in case yields no
        conflict, and the short name claims the long company's article.
        """
        registry = NaverNewsCollector.registry(["SK", "Sk하이닉스"])

        assert NaverNewsCollector.conflicts_for(("SK",), registry) == ("Sk하이닉스",)
        assert (
            NaverNewsCollector.match_method(
                "SK하이닉스 HBM 증설", name="SK", conflicts=("Sk하이닉스",)
            )
            is None
        )


class TestWhereASpaceMayFall:
    """A space is tolerated where the script changes, not between syllables.

    `SK 하이닉스` and `SK하이닉스` are the same company and copy uses both. But
    allowing a space between two Hangul syllables makes every short name match
    ordinary prose: `한 화면에` becomes 한화 and `최 대 유 통 업체` becomes
    대유. Those are real Korean sentences, and the names they damage are the
    short well-known ones that appear most often.

    The cost is the reverse spelling — `삼성 전자` for 삼성전자 — which Korean
    copy does not normally use. Worth measuring against the reject rate on the
    first full sweep rather than assuming.
    """

    def test_a_space_at_a_script_change_is_allowed(self) -> None:
        for text in ("SK 하이닉스 신고가", "SK하이닉스 신고가"):
            assert NaverNewsCollector.match_method(text, name="SK하이닉스") is MatchMethod.NAME

    def test_a_space_between_syllables_is_not_a_company(self) -> None:
        assert NaverNewsCollector.match_method("한 화면에 담았다", name="한화") is None
        assert NaverNewsCollector.match_method("최 대 유 통 업체", name="대유") is None

    def test_the_solid_spelling_still_matches(self) -> None:
        assert NaverNewsCollector.match_method("한화는 실적을", name="한화") is MatchMethod.NAME

    def test_punctuation_still_takes_a_space(self) -> None:
        assert NaverNewsCollector.match_method("KT & G 매출", name="KT&G") is MatchMethod.NAME


class TestWhatCameBackHasToBeTheShapeItClaims:
    """Naver's answer is a stranger's JSON, and `.get` is not a type check.

    A body that parses but is the wrong shape raises `AttributeError` or
    `TypeError` deep inside the sweep. Neither is a `CollectorError`, so
    `run_collector` re-raises and records the run as a defect in our code —
    an outage filed as a bug, which is the one distinction the error taxonomy
    exists to make. The listing master turns this collector loose on nearly
    four thousand names, so a bad answer to one of them must not end the run.
    """

    @staticmethod
    def serving(body: object) -> tuple[NaverNewsCollector, httpx.Client]:
        c = NaverNewsCollector(guard=FakeGuard(), max_pages=2)  # type: ignore[arg-type]
        c._client_id = "id"
        c._client_secret = "secret"
        c._bucket = type("NoWait", (), {"acquire": lambda self: None})()  # type: ignore[assignment]
        return c, httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        )

    def test_a_payload_that_is_not_an_object_is_an_outage(self) -> None:
        c, client = self.serving([1, 2, 3])
        with client, pytest.raises(UpstreamUnavailableError, match="not an object"):
            c._get(client, query="삼성전자", start=1)

    def test_results_that_are_not_a_list_are_an_outage(self) -> None:
        c, client = self.serving({"items": "삼성전자 기사 하나"})
        with client, pytest.raises(UpstreamUnavailableError, match="where rows were expected"):
            c._sweep(client, query="삼성전자", since=SINCE)

    def test_a_result_that_is_not_an_object_is_counted_unusable(self) -> None:
        """One malformed entry costs that entry, the way a missing date does."""
        rows, skipped, _ = NaverNewsCollector.to_rows(
            ["nonsense", 7, item(originallink="https://e.com/ok")],  # type: ignore[list-item]
            since=None,
        )

        assert len(rows) == 1
        assert skipped == 2

    def test_a_field_inside_a_good_item_may_still_be_wrong(self) -> None:
        """The guard on the item is not a guard on its fields.

        This is the shape that has reopened five times, and the last instance
        was in this very function: `link` was read raw while every field beside
        it was normalised. A number there ends the sweep before its commit, so
        every company collected earlier in the run is discarded with it.
        """
        rows, skipped, _ = NaverNewsCollector.to_rows(
            [
                {
                    "title": "삼성전자 실적",
                    "description": "본문",
                    "originallink": "https://news.example.com/a",
                    "link": 12345,
                    "pubDate": "Mon, 21 Sep 2026 14:03:00 +0900",
                }
            ],
            since=None,
        )

        assert len(rows) == 1
        assert skipped == 0
        assert rows[0][0].naver_url is None

    def test_a_page_of_nothing_but_rubbish_does_not_raise(self) -> None:
        c, client = self.serving({"items": ["rubbish", 1, None]})
        with client:
            sweep = c._sweep(client, query="삼성전자", since=SINCE)

        assert sweep.rows == []
        assert sweep.skipped == 3


class TestTheRejectReport:
    """The number the staged rollout is gated on, per company.

    One company, then ten, then all of them — and at each step the question is
    which companies' searches came back full of articles that never named them.
    A total cannot answer that. Rejected articles leave no mention behind, so
    the per-company figure cannot be rebuilt from the database later either;
    it exists only if the run writes it down.
    """

    def test_the_overall_rate_leads(self) -> None:
        report = NaverNewsCollector.reject_report(
            [Yield("삼성전자", "005930", matched=6, rejected=4)]
        )

        assert report.startswith("reject rate 4/10 (40%)")

    def test_the_worst_company_comes_first(self) -> None:
        report = NaverNewsCollector.reject_report(
            [
                Yield("삼성전자", "005930", matched=9, rejected=1),
                Yield("NAVER", "035420", matched=1, rejected=9),
                Yield("한화", "000880", matched=5, rejected=5),
            ]
        )

        worst = report.split("highest reject rate: ")[1].split(";")[0]
        assert worst.index("NAVER") < worst.index("한화") < worst.index("삼성전자")
        assert "NAVER(035420) 9/10 90%" in worst

    def test_the_rate_ranks_the_list_not_the_count(self) -> None:
        """A small company that fails every search outranks a big one that
        fails a few. Ordering by count would bury exactly the queries that are
        broken outright under the ones that are merely busy."""
        report = NaverNewsCollector.reject_report(
            [
                Yield("대형주", "000001", matched=12, rejected=8),
                Yield("소형주", "000002", matched=0, rejected=3),
            ]
        )

        worst = report.split("highest reject rate: ")[1].split(";")[0]
        assert worst.index("소형주") < worst.index("대형주")

    def test_a_company_with_no_rejects_is_not_on_the_worst_list(self) -> None:
        report = NaverNewsCollector.reject_report(
            [
                Yield("삼성전자", "005930", matched=10, rejected=0),
                Yield("NAVER", "035420", matched=1, rejected=1),
            ]
        )

        worst = report.split("highest reject rate: ")[1].split(";")[0]
        assert "삼성전자" not in worst

    def test_many_mentions_with_no_reject_is_its_own_list(self) -> None:
        """The other outlier: a false match passing straight through looks
        exactly like a popular company with a perfect record."""
        report = NaverNewsCollector.reject_report(
            [
                Yield("삼성전자", "005930", matched=40, rejected=0),
                Yield("LG", "003550", matched=90, rejected=0),
                Yield("NAVER", "035420", matched=1, rejected=1),
            ]
        )

        loud = report.split("most mentions with no reject: ")[1]
        assert loud.index("LG") < loud.index("삼성전자")
        assert "NAVER" not in loud

    def test_each_list_stops_at_the_limit(self) -> None:
        yields = [
            Yield(f"회사{i:02d}", f"9{i:05d}", matched=1, rejected=1 + i)
            for i in range(REPORT_TOP + 15)
        ]
        report = NaverNewsCollector.reject_report(yields)

        worst = report.split("highest reject rate: ")[1].split(";")[0]
        assert worst.count("/") == REPORT_TOP

    def test_nothing_evaluated_says_so(self) -> None:
        """A sweep whose every company returned nothing is not a 0% reject rate."""
        assert NaverNewsCollector.reject_report([]) == "no articles evaluated"
        assert (
            NaverNewsCollector.reject_report([Yield("삼성전자", "005930", 0, 0)])
            == "no articles evaluated"
        )

    def test_a_rerun_is_not_a_company_whose_every_result_failed(self) -> None:
        """Matched counts articles that name the company, new or not.

        A second run over the same window inserts no mentions. Counting only
        the inserted ones would show every company as matched zero and make
        the reject rate meaningless on exactly the runs that repeat.
        """
        report = NaverNewsCollector.reject_report(
            [Yield("삼성전자", "005930", matched=10, rejected=0)]
        )

        assert report.startswith("reject rate 0/10 (0%)")
