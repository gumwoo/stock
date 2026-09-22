"""Articles stored once, mentions earned, and the watermark that must not slip.

Three things here cannot be checked without a database, and each one is a
defect that would otherwise ship looking fine.

An article matching two companies has to end up as one row with two mentions.
The insert alone cannot produce that: `ON CONFLICT DO NOTHING ... RETURNING id`
returns only rows it inserted, and the second company's mention needs the id of
a row it did not insert. That is the common case once any two instruments share
a sector, so the test for it is the one that keeps the bug out.

A mention has to be earned. A search hit is not evidence the article is about
the company, and storing it on that basis would attach a sentiment score to the
wrong company later — a number that looks entirely ordinary.

The watermark has to come from a run that finished. `last_success` counts
PARTIAL runs, so a sweep that covered a third of the market and stopped would
still push the watermark forward, and everything it never reached would skip
that window permanently.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.base import CollectorStatusLookup, run_collector
from app.collectors.naver_news import PAGE_SIZE, NaverNewsCollector
from app.config import get_settings
from app.core.calendar import Market
from app.models import Base, CollectorRun, CollectorStatus, Instrument, SymbolHistory
from app.models.news import MatchMethod, NewsItem, NewsMention

pytestmark = pytest.mark.integration

HOST = "news-test-fixture.example.com"
SOURCE = "NAVER_NEWS_TEST"
NOW = datetime(2026, 9, 22, 5, 0, tzinfo=UTC)


class FakeGuard:
    """Reservations are exercised elsewhere; here they must simply not block."""

    def __init__(self) -> None:
        self.reserved = 0

    def reserve(self, group: str, endpoint: str, *, calls: int = 1, now: Any = None) -> None:
        self.reserved += 1


def article(*, slug: str, title: str, summary: str = "") -> dict[str, str]:
    return {
        "title": title,
        "description": summary,
        "originallink": f"https://{HOST}/{slug}",
        "link": f"https://n.news.naver.com/{slug}",
        "pubDate": "Mon, 21 Sep 2026 14:03:00 +0900",
    }


@pytest.fixture(scope="module")
def engine() -> Iterator[object]:
    eng = create_engine(get_settings().database_url, future=True)
    try:
        with eng.connect():
            pass
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def market(engine: object) -> Iterator[tuple[Session, list[Instrument]]]:
    """Korean companies an article can name, including a pair that collide.

    `테스트반도체소재` contains `테스트반도체`, which is the shape 252 of the
    3,961 real listed names have. Without it in the database the swallowing
    rule could be correct in `match_method` and never reach the collector.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        made: list[Instrument] = []
        for name, symbol in (
            ("테스트반도체", "900001"),
            ("테스트화학", "900002"),
            ("테스트반도체소재", "900003"),
        ):
            inst = Instrument(market=Market.KR, name=name, tracked=False)
            s.add(inst)
            s.flush()
            s.add(
                SymbolHistory(
                    instrument_id=inst.instrument_id,
                    symbol=symbol,
                    valid_from=datetime(2000, 1, 1, tzinfo=UTC).date(),
                    source="SEED",
                )
            )
            made.append(inst)
        s.commit()

        yield s, made

        ids = [i.instrument_id for i in made]
        s.execute(
            text(
                "DELETE FROM news_mention WHERE instrument_id = ANY(:ids) "
                "OR news_item_id IN (SELECT id FROM news_item WHERE url LIKE :host)"
            ),
            {"ids": ids, "host": f"%{HOST}%"},
        )
        s.execute(text("DELETE FROM news_item WHERE url LIKE :host"), {"host": f"%{HOST}%"})
        s.execute(text("DELETE FROM symbol_history WHERE instrument_id = ANY(:ids)"), {"ids": ids})
        s.execute(text("DELETE FROM instrument WHERE instrument_id = ANY(:ids)"), {"ids": ids})
        s.execute(text("DELETE FROM collector_run WHERE source = :src"), {"src": SOURCE})
        s.commit()


def collector_over(pages: dict[str, list[list[dict[str, str]]]]) -> NaverNewsCollector:
    """A collector whose transport answers from a script, keyed by query."""
    c = NaverNewsCollector(guard=FakeGuard(), max_pages=2)  # type: ignore[arg-type]
    # A test-only source name, set here rather than in the three tests that
    # remembered to. Left as NAVER_NEWS these read the real collector_run
    # history: one genuine collection in the development database was enough to
    # push the watermark past every fixture article and turn eight of these
    # red, which makes the gate report the database's state and not the code's.
    c.name = SOURCE
    # The transport is faked below, so the key is never used — but without one
    # `run_collector` records SKIPPED and the tests that assert on a status
    # fail wherever `.env` is absent, which is every CI runner.
    c._client_id = "test-key-id"
    c._client_secret = "test-key"

    def fake_get(client: Any, *, query: str, start: int) -> dict[str, Any]:
        index = (start - 1) // PAGE_SIZE
        script = pages.get(query, [])
        return {"items": script[index] if index < len(script) else []}

    c._get = fake_get  # type: ignore[assignment,method-assign]
    return c


def items(session: Session) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(NewsItem).where(NewsItem.url.like(f"%{HOST}%"))
        ).scalar_one()
    )


def mentions(session: Session, instrument_id: int) -> list[NewsMention]:
    return list(
        session.execute(
            select(NewsMention).where(NewsMention.instrument_id == instrument_id)
        ).scalars()
    )


class TestOneArticleManyCompanies:
    def test_a_shared_article_is_stored_once_with_two_mentions(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """The reason the schema is two tables.

        Stored per instrument, this article's text would exist twice, and the
        sentiment scorer would read and bill for the same words twice — and
        could return two different verdicts on them.
        """
        session, (semi, chem, _) = market
        shared = article(
            slug="shared",
            title="테스트반도체와 테스트화학, 공동 투자 발표",
            summary="두 회사가 합작법인을 세운다",
        )
        c = collector_over({"테스트반도체": [[shared]], "테스트화학": [[shared]]})

        c.collect(session)

        assert items(session) == 1
        assert len(mentions(session, semi.instrument_id)) == 1
        assert len(mentions(session, chem.instrument_id)) == 1

    def test_a_mention_attaches_to_an_article_this_run_did_not_insert(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """The trap the `filing_repo` template does not cover.

        `RETURNING id` yields only newly inserted rows. The second company's
        mention needs the id of a row that already existed, so the repository
        has to look ids up rather than rely on what the insert handed back.
        Without that lookup this is silently zero mentions.
        """
        session, (_, chem, _material) = market
        shared = article(slug="shared", title="테스트반도체와 테스트화학 합작")

        collector_over({"테스트반도체": [[shared]]}).collect(session)
        assert items(session) == 1

        collector_over({"테스트화학": [[shared]]}).collect(session)

        assert items(session) == 1
        assert len(mentions(session, chem.instrument_id)) == 1

    def test_collecting_the_same_page_twice_changes_nothing(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        session, (semi, *_) = market
        page = {"테스트반도체": [[article(slug="a", title="테스트반도체 실적")]]}

        collector_over(page).collect(session)
        collector_over(page).collect(session)

        assert items(session) == 1
        assert len(mentions(session, semi.instrument_id)) == 1


class TestMentionsAreEarned:
    def test_an_article_that_never_names_the_company_gets_no_mention(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """Naver matches related terms, so the query can return this.

        The article is still stored — it is a real article, and another company
        may be in it — but nothing claims it is about this one.
        """
        session, (semi, *_) = market
        c = collector_over(
            {"테스트반도체": [[article(slug="unrelated", title="반도체 업황 회복 조짐")]]}
        )

        result = c.collect(session)

        assert items(session) == 1
        assert mentions(session, semi.instrument_id) == []
        assert "1 rejected" in (result.detail or "")

    def test_a_mention_records_what_matched(self, market: tuple[Session, list[Instrument]]) -> None:
        """So a link made on a looser rule can be found again later."""
        session, (semi, *_) = market
        collector_over(
            {"테스트반도체": [[article(slug="named", title="테스트반도체 3분기 실적")]]}
        ).collect(session)

        (mention,) = mentions(session, semi.instrument_id)

        assert mention.match_method is MatchMethod.NAME
        assert mention.matched_query == "테스트반도체"

    def test_the_summary_counts_as_naming_the_company(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        session, (semi, *_) = market
        collector_over(
            {
                "테스트반도체": [
                    [article(slug="insummary", title="업계 재편", summary="테스트반도체가 인수")]
                ]
            }
        ).collect(session)

        assert len(mentions(session, semi.instrument_id)) == 1


class TestTheWatermark:
    def test_a_first_run_reads_a_bounded_window(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        session, _ = market
        c = NaverNewsCollector(guard=FakeGuard())  # type: ignore[arg-type]
        c.name = SOURCE

        since = c.watermark(session, now=NOW)

        assert NOW - since == timedelta(days=3)

    def test_a_partial_run_does_not_move_the_watermark(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """The defect this guards against.

        A sweep that covered a third of the market and stopped would, if its
        finish time became the next watermark, make every instrument it never
        reached skip that window — silently, and for good.
        """
        session, _ = market
        session.add(
            CollectorRun(
                source=SOURCE,
                started_at=NOW - timedelta(hours=5),
                finished_at=NOW - timedelta(hours=4),
                status=CollectorStatus.PARTIAL,
            )
        )
        session.commit()

        c = NaverNewsCollector(guard=FakeGuard())  # type: ignore[arg-type]
        c.name = SOURCE

        assert NOW - c.watermark(session, now=NOW) == timedelta(days=3)

    def test_a_complete_run_does_move_it(self, market: tuple[Session, list[Instrument]]) -> None:
        session, _ = market
        finished = NOW - timedelta(hours=4)
        session.add(
            CollectorRun(
                source=SOURCE,
                started_at=NOW - timedelta(hours=5),
                finished_at=finished,
                status=CollectorStatus.SUCCESS,
            )
        )
        session.commit()

        c = NaverNewsCollector(guard=FakeGuard())  # type: ignore[arg-type]
        c.name = SOURCE

        # Backed off a couple of hours, so an article published while the last
        # sweep was mid-flight does not fall between two touching windows.
        assert c.watermark(session, now=NOW) == finished - timedelta(hours=2)

    def test_a_long_silence_does_not_let_the_window_grow_without_limit(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """Otherwise a stalled watermark makes every run re-read more history."""
        session, _ = market
        session.add(
            CollectorRun(
                source=SOURCE,
                started_at=NOW - timedelta(days=40),
                finished_at=NOW - timedelta(days=40),
                status=CollectorStatus.SUCCESS,
            )
        )
        session.commit()

        c = NaverNewsCollector(guard=FakeGuard())  # type: ignore[arg-type]
        c.name = SOURCE

        assert NOW - c.watermark(session, now=NOW) == timedelta(days=7)


class TestReach:
    def test_untracked_names_are_swept_too(self, market: tuple[Session, list[Instrument]]) -> None:
        """News is how a company becomes worth tracking, so collection cannot
        be limited to the ones already tracked. Scoring is the other way round.
        """
        session, made = market
        assert all(not i.tracked for i in made)

        result = collector_over(
            {"테스트반도체": [[article(slug="a", title="테스트반도체 실적")]]}
        ).collect(session)

        assert "instruments" in (result.detail or "")
        assert items(session) == 1


class TestATruncatedRunIsNotComplete:
    """Whatever the sweep did not read, the watermark must not step over.

    The watermark is global, so it cannot say "covered for these names and not
    those". A run that skipped instruments, or stopped two pages into one,
    therefore has only one honest thing to report: it did not finish. Recorded
    as SUCCESS it moves the window forward for all 2,500 names, and the range
    it never read is gone for good.

    This is not hypothetical. A `--limit 1` rehearsal in the development
    database finished SUCCESS and cut the next full run's window from three
    days to fourteen hours.
    """

    def test_limiting_the_instruments_makes_the_run_partial(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        session, _ = market
        c = collector_over({"테스트반도체": [[article(slug="one", title="테스트반도체 소식")]]})
        c.max_instruments = 1

        result = c.collect(session)

        assert result.partial is True
        assert any("never asked about" in w for w in result.warnings)

    def test_hitting_the_page_cap_makes_the_run_partial(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """Bounded cost, unbounded loss, until a per-instrument cursor exists."""
        session, _ = market
        page = [article(slug=f"cap-{i}", title="테스트반도체 실적 발표") for i in range(PAGE_SIZE)]
        c = collector_over({"테스트반도체": [page]})
        c.max_pages = 1

        result = c.collect(session)

        assert result.partial is True
        assert any("page cap" in w for w in result.warnings)

    def test_a_truncated_run_leaves_the_watermark_where_it_was(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        session, _ = market
        c = collector_over({"테스트반도체": [[article(slug="one", title="테스트반도체 소식")]]})
        c.max_instruments = 1

        run = run_collector(c, session)

        assert run.status is CollectorStatus.PARTIAL
        assert CollectorStatusLookup(session).last_full_success(SOURCE) is None

    def test_a_full_sweep_is_still_a_success(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """The control. Without it the rule above could be satisfied by never
        reporting success at all, which would pin the window open forever."""
        session, _ = market
        c = collector_over({"테스트반도체": [[article(slug="one", title="테스트반도체 소식")]]})

        run = run_collector(c, session)

        assert run.status is CollectorStatus.SUCCESS
        assert CollectorStatusLookup(session).last_full_success(SOURCE) is not None


class TestTheLongerNameWinsInTheDatabase:
    def test_an_article_about_the_longer_company_is_not_the_shorter_one(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """The rule has to reach the collector, not just the pure function.

        `테스트반도체` is a substring of `테스트반도체소재`, so the search for
        the short name returns the long company's article. The conflict set is
        built from the whole instrument master inside `collect`; if that wiring
        were missing, this stores a mention against the wrong company and looks
        completely normal afterwards.
        """
        session, (semi, _, material) = market
        piece = article(slug="material", title="테스트반도체소재 신규 공장 착공")
        c = collector_over({"테스트반도체": [[piece]], "테스트반도체소재": [[piece]]})

        c.collect(session)

        assert items(session) == 1
        assert mentions(session, semi.instrument_id) == []
        assert len(mentions(session, material.instrument_id)) == 1


class TestNothingToSweepIsNotASweep:
    def test_an_empty_universe_does_not_advance_the_watermark(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """Migrated but not yet seeded is a real state, and a dangerous one.

        It is the state a few minutes before thousands of names arrive. A run
        that reports SUCCESS there pins the watermark to now, and everything
        those names were written about in the previous three days is stepped
        over and never read.
        """
        session, _ = market
        c = collector_over({})

        def nothing(*_: object, **__: object) -> list[Instrument]:
            return []

        import app.collectors.naver_news as module

        original = module.instrument_repo.list_active
        module.instrument_repo.list_active = nothing  # type: ignore[assignment]
        try:
            run = run_collector(c, session)
        finally:
            module.instrument_repo.list_active = original  # type: ignore[assignment]

        assert run.status is CollectorStatus.SKIPPED
        assert CollectorStatusLookup(session).last_full_success(SOURCE) is None


class TestAZeroPageBudgetIsRefused:
    def test_zero_pages_cannot_be_configured(self) -> None:
        """It reads nothing and `_sweep` would call that a clean finish."""
        with pytest.raises(ValueError, match="at least 1"):
            NaverNewsCollector(guard=FakeGuard(), max_pages=0)  # type: ignore[arg-type]
