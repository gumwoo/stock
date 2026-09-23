"""The query-hit layer: every search result kept, with the verdict on it.

`news_item` is what was published, `news_query_hit` is which company's search
surfaced it and whether it is about that company, and `news_mention` holds
only the confirmed ones. These tests pin the three properties the layer exists
for: the mention table always equals the confirmed hits, a later verdict can
take a mention away, and a rule never overrules a model.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.naver_news import RULE_VERSION, NaverNewsCollector, rejudge_hits
from app.config import get_settings
from app.core.calendar import Market
from app.models import Base, Instrument, SymbolHistory
from app.models.news import (
    Decider,
    HitDecision,
    MatchMethod,
    NewsItem,
    NewsMention,
    NewsQueryHit,
    NewsSource,
)
from app.repositories import news_repo
from app.repositories.news_repo import QueryHitRow

pytestmark = pytest.mark.integration

HOST = "query-hit-fixture.example.com"
T0 = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
# Two syllables no listed company uses, so the rule treats it as ambiguous and
# no real name in the master swallows it.
SHORT = "쀓쀚"


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
def world(engine: object) -> Iterator[tuple[Session, Instrument, list[int]]]:
    """One ambiguous-named company and three articles about it, or not."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.KR, name=SHORT, tracked=False)
        s.add(inst)
        s.flush()
        s.add(
            SymbolHistory(
                instrument_id=inst.instrument_id,
                symbol="990901",
                valid_from=datetime(2000, 1, 1, tzinfo=UTC).date(),
                source="SEED",
            )
        )
        items = []
        for slug, title in (
            ("lead", SHORT + ", 신규 수주 공시"),
            ("garden", "산책하기 좋은 " + SHORT + " 이야기"),
            ("absent", "반도체 업황 회복"),
        ):
            item = NewsItem(
                source=NewsSource.NAVER_NEWS,
                url_hash=(slug + "-" + HOST).ljust(64, "0")[:64],
                url=f"https://{HOST}/{slug}",
                title=title,
                summary="",
                published_at=T0,
                available_at=T0,
            )
            s.add(item)
            s.flush()
            items.append(item.id)
        s.commit()
        try:
            yield s, inst, items
        finally:
            s.rollback()
            s.execute(text("DELETE FROM news_item WHERE url LIKE :h"), {"h": f"%{HOST}%"})
            s.execute(
                text("DELETE FROM symbol_history WHERE instrument_id = :i"),
                {"i": inst.instrument_id},
            )
            s.execute(
                text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": inst.instrument_id}
            )
            s.commit()


def hit(
    item: int,
    inst: int,
    decision: HitDecision,
    *,
    by: Decider = Decider.RULE,
    at: datetime = T0,
    version: int = RULE_VERSION,
    snippet: str | None = "",
) -> QueryHitRow:
    return QueryHitRow(
        news_item_id=item,
        instrument_id=inst,
        matched_query=SHORT,
        decision=decision,
        decision_reason="test",
        match_method=None if decision is HitDecision.REJECTED else MatchMethod.NAME,
        snippet=snippet,
        rule_version=version,
        decided_by=by,
        decided_at=at,
    )


def mention_pairs(session: Session, inst: int) -> set[int]:
    return set(
        session.execute(
            select(NewsMention.news_item_id).where(NewsMention.instrument_id == inst)
        ).scalars()
    )


def stored(session: Session, item: int, inst: int) -> NewsQueryHit:
    session.expire_all()
    return session.execute(
        select(NewsQueryHit).where(
            NewsQueryHit.news_item_id == item, NewsQueryHit.instrument_id == inst
        )
    ).scalar_one()


class TestTheMentionTableIsAProjection:
    def test_only_confirmed_hits_become_mentions(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        session, inst, (lead, garden, absent) = world
        news_repo.record_hits(
            session,
            [
                hit(lead, inst.instrument_id, HitDecision.CONFIRMED),
                hit(garden, inst.instrument_id, HitDecision.PENDING),
                hit(absent, inst.instrument_id, HitDecision.REJECTED),
            ],
        )
        session.commit()

        assert mention_pairs(session, inst.instrument_id) == {lead}
        assert news_repo.projection_drift(session) == (0, 0)

    def test_a_verdict_that_changes_its_mind_takes_the_mention_away(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        session, inst, (lead, _, _) = world
        news_repo.record_hits(session, [hit(lead, inst.instrument_id, HitDecision.CONFIRMED)])
        session.commit()
        written = news_repo.record_hits(
            session, [hit(lead, inst.instrument_id, HitDecision.PENDING)]
        )
        session.commit()

        assert written.mentions_removed == 1
        assert mention_pairs(session, inst.instrument_id) == set()
        assert news_repo.projection_drift(session) == (0, 0)

    def test_the_same_pair_twice_in_one_batch_keeps_the_last(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        """Two pages can surface one article; one statement cannot update a row twice."""
        session, inst, (lead, _, _) = world
        news_repo.record_hits(
            session,
            [
                hit(lead, inst.instrument_id, HitDecision.PENDING),
                hit(lead, inst.instrument_id, HitDecision.CONFIRMED),
            ],
        )
        session.commit()

        assert stored(session, lead, inst.instrument_id).decision is HitDecision.CONFIRMED


class TestWhatAVerdictRead:
    def test_a_later_sweep_replaces_the_snippet_with_its_own(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        """The stored snippet must be the one the stored verdict read."""
        session, inst, (lead, _, _) = world
        news_repo.record_hits(
            session, [hit(lead, inst.instrument_id, HitDecision.PENDING, snippet="first")]
        )
        session.commit()
        news_repo.record_hits(
            session, [hit(lead, inst.instrument_id, HitDecision.CONFIRMED, snippet="second")]
        )
        session.commit()

        assert stored(session, lead, inst.instrument_id).snippet == "second"


class TestWhenAVerdictWasReached:
    def test_the_same_verdict_again_keeps_its_time(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        """A re-judgment that agrees is not a new decision."""
        session, inst, (lead, _, _) = world
        news_repo.record_hits(session, [hit(lead, inst.instrument_id, HitDecision.CONFIRMED)])
        session.commit()
        news_repo.record_hits(
            session,
            [hit(lead, inst.instrument_id, HitDecision.CONFIRMED, at=T0 + timedelta(days=3))],
        )
        session.commit()

        assert stored(session, lead, inst.instrument_id).decided_at == T0

    def test_a_changed_verdict_moves_it(self, world: tuple[Session, Instrument, list[int]]) -> None:
        """A confirmation reached later did not exist earlier."""
        session, inst, (_, garden, _) = world
        later = T0 + timedelta(days=3)
        news_repo.record_hits(session, [hit(garden, inst.instrument_id, HitDecision.PENDING)])
        session.commit()
        news_repo.record_hits(
            session, [hit(garden, inst.instrument_id, HitDecision.CONFIRMED, at=later)]
        )
        session.commit()

        assert stored(session, garden, inst.instrument_id).decided_at == later


class TestARuleNeverOverrulesAModel:
    def test_a_later_sweep_does_not_undo_a_model_verdict(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        session, inst, (_, garden, _) = world
        news_repo.record_hits(
            session,
            [hit(garden, inst.instrument_id, HitDecision.CONFIRMED, by=Decider.LLM)],
        )
        session.commit()
        news_repo.record_hits(session, [hit(garden, inst.instrument_id, HitDecision.PENDING)])
        session.commit()

        row = stored(session, garden, inst.instrument_id)
        assert row.decision is HitDecision.CONFIRMED
        assert row.decided_by is Decider.LLM
        assert mention_pairs(session, inst.instrument_id) == {garden}

    def test_a_model_may_overrule_a_rule(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        session, inst, (_, garden, _) = world
        news_repo.record_hits(session, [hit(garden, inst.instrument_id, HitDecision.PENDING)])
        session.commit()
        news_repo.record_hits(
            session,
            [hit(garden, inst.instrument_id, HitDecision.CONFIRMED, by=Decider.LLM)],
        )
        session.commit()

        assert stored(session, garden, inst.instrument_id).decided_by is Decider.LLM
        assert mention_pairs(session, inst.instrument_id) == {garden}


class TestRejudgingStoredHits:
    """The migration moved old mentions in at rule version 0; this re-decides them."""

    def test_a_garden_confirmed_by_the_old_rule_is_withdrawn(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        session, inst, (lead, garden, _) = world
        news_repo.record_hits(
            session,
            [
                hit(lead, inst.instrument_id, HitDecision.CONFIRMED, version=0),
                hit(garden, inst.instrument_id, HitDecision.CONFIRMED, version=0),
            ],
        )
        session.commit()
        assert mention_pairs(session, inst.instrument_id) == {lead, garden}

        result = rejudge_hits(session, instrument_ids=[inst.instrument_id])

        assert result.judged == 2
        assert mention_pairs(session, inst.instrument_id) == {lead}
        assert stored(session, garden, inst.instrument_id).decision is HitDecision.PENDING
        assert stored(session, lead, inst.instrument_id).rule_version == RULE_VERSION
        assert news_repo.projection_drift(session) == (0, 0)

    def test_a_model_verdict_is_not_rejudged(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        session, inst, (_, garden, _) = world
        news_repo.record_hits(
            session,
            [hit(garden, inst.instrument_id, HitDecision.CONFIRMED, by=Decider.LLM, version=0)],
        )
        session.commit()

        result = rejudge_hits(session, instrument_ids=[inst.instrument_id])

        assert result.judged == 0
        assert stored(session, garden, inst.instrument_id).decision is HitDecision.CONFIRMED

    def test_the_snippet_the_verdict_read_is_what_it_is_judged_by(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        """Not the article's summary, which another search may have written.

        Naver cuts the snippet around the query. The article's stored summary
        is whichever search reached it first, and here names nobody; judged by
        it, a confirmed hit would turn into "absent" on a rule change alone.
        """
        session, inst, (_, _, absent) = world
        session.execute(
            text("UPDATE news_item SET summary = :s WHERE id = :i"),
            {"s": "메모리 가격 반등", "i": absent},
        )
        news_repo.record_hits(
            session,
            [
                hit(
                    absent,
                    inst.instrument_id,
                    HitDecision.CONFIRMED,
                    version=0,
                    snippet="(주)" + SHORT + " 측은 증설을 검토 중이다",
                )
            ],
        )
        session.commit()

        result = rejudge_hits(session, instrument_ids=[inst.instrument_id])

        row = stored(session, absent, inst.instrument_id)
        assert result.judged == 1
        assert (row.decision, row.decision_reason) == (
            HitDecision.CONFIRMED,
            "strong:corporate_mark",
        )

    def test_a_hit_with_no_snippet_is_left_as_it_was(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        """Stored before hits kept their snippet: what it read is unknown."""
        session, inst, (_, garden, _) = world
        news_repo.record_hits(
            session,
            [hit(garden, inst.instrument_id, HitDecision.CONFIRMED, version=0, snippet=None)],
        )
        session.commit()

        result = rejudge_hits(session, instrument_ids=[inst.instrument_id])

        row = stored(session, garden, inst.instrument_id)
        assert (result.judged, result.unread) == (0, 1)
        assert (row.decision, row.rule_version) == (HitDecision.CONFIRMED, 0)
        assert mention_pairs(session, inst.instrument_id) == {garden}

    def test_a_current_verdict_is_left_alone(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        session, inst, (lead, _, _) = world
        news_repo.record_hits(session, [hit(lead, inst.instrument_id, HitDecision.CONFIRMED)])
        session.commit()

        assert rejudge_hits(session, instrument_ids=[inst.instrument_id]).judged == 0


class TestTheSweepWritesVerdicts:
    """The collector itself must judge, not just the pure function.

    Every other sweep test uses long company names, which the rule confirms on
    sight — so a collector that ignored the verdict and linked everything
    would pass them all. This one sweeps an ambiguous name.
    """

    def test_a_garden_is_kept_as_pending_and_not_linked(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        session, inst, _ = world
        pub = "Mon, 21 Sep 2026 14:03:00 +0900"
        items = [
            {
                "title": SHORT + ", 신규 공장 착공",
                "description": "<b>" + SHORT + "</b> 측은 착공식을 연다",
                "originallink": f"https://{HOST}/sweep-lead",
                "link": "",
                "pubDate": pub,
            },
            {
                "title": "가을 산책길 " + SHORT + " 풍경",
                "description": "",
                "originallink": f"https://{HOST}/sweep-garden",
                "link": "",
                "pubDate": pub,
            },
        ]

        class Guard:
            def reserve(
                self, group: str, endpoint: str, *, calls: int = 1, now: Any = None
            ) -> None:
                return None

        c = NaverNewsCollector(guard=Guard(), only=[SHORT], max_pages=1)  # type: ignore[arg-type]
        c.name = "NAVER_NEWS_HIT_TEST"
        c._client_id = "id"
        c._client_secret = "secret"
        c._get = lambda client, *, query, start: {"items": items if query == SHORT else []}  # type: ignore[assignment,method-assign]

        try:
            result = c.collect(session)
        finally:
            session.execute(
                text("DELETE FROM collector_run WHERE source = :s"), {"s": "NAVER_NEWS_HIT_TEST"}
            )
            session.commit()

        verdicts = {
            row.news_item_id: row.decision
            for row in session.execute(
                select(NewsQueryHit).where(NewsQueryHit.instrument_id == inst.instrument_id)
            ).scalars()
        }
        lead_id, garden_id = (
            session.execute(
                select(NewsItem.id).where(NewsItem.url == f"https://{HOST}/{slug}")
            ).scalar_one()
            for slug in ("sweep-lead", "sweep-garden")
        )

        assert verdicts == {lead_id: HitDecision.CONFIRMED, garden_id: HitDecision.PENDING}
        # What the verdict read, kept so a later rule reads the same text.
        assert stored(session, lead_id, inst.instrument_id).snippet == SHORT + " 측은 착공식을 연다"
        assert mention_pairs(session, inst.instrument_id) == {lead_id}
        assert "1 pending" in (result.detail or "")
        assert news_repo.projection_drift(session) == (0, 0)


class TestAFullSweepFitsInOneCall:
    def test_nine_thousand_pairs_are_written_and_withdrawn(
        self, world: tuple[Session, Instrument, list[int]]
    ) -> None:
        """A full sweep records about 30,000 hits at once.

        The mention sync matches `(item, instrument)` pairs with a tuple list,
        and Postgres refused 8,000 of them in one statement. All PENDING, so
        the withdrawal path meets the same number the lookup does.
        """
        session, inst, _ = world
        count = 9_000
        rows = [
            news_repo.NewsItemRow(
                source=NewsSource.NAVER_NEWS,
                url_hash=f"bulk-{n}-{HOST}".ljust(64, "0")[:64],
                url=f"https://{HOST}/bulk/{n}",
                naver_url=None,
                publisher_host=HOST,
                title="bulk",
                summary=None,
                published_at=T0,
                available_at=T0,
            )
            for n in range(count)
        ]
        _, ids = news_repo.save_news_items(session, rows)
        session.commit()
        assert len(ids) == count

        written = news_repo.record_hits(
            session, [hit(i, inst.instrument_id, HitDecision.PENDING) for i in ids.values()]
        )
        session.commit()

        assert written == news_repo.HitWrite(0, 0)
        assert news_repo.projection_drift(session) == (0, 0)
