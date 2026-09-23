"""Discovery and promotion against the database.

Every assertion here is about what could be known at a moment: verdicts as
they stood, articles stored by then, stretches a sweep had read by then, and
which names were tracked then. The rest of the database may hold real news;
the tests look only at their own instruments.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.naver_news import RULE_VERSION
from app.config import get_settings
from app.core.calendar import Market
from app.core.types import Freshness
from app.models import Base, Instrument, SymbolHistory
from app.models.collector import CollectorStatus
from app.models.news import Decider, HitDecision, MatchMethod, NewsItem, NewsSource
from app.models.promotion import InstrumentPromotion
from app.repositories import news_repo, promotion_repo
from app.repositories.news_repo import CoverageRow, QueryHitRow
from app.services import discovery_service, promotion_service

pytestmark = pytest.mark.integration

HOST = "discovery-fixture.example.com"
# Names no listed company uses.
RISING, STEADY, UNREAD, FOLLOWED = "쀓발굴상승", "쀓발굴보합", "쀓발굴미독", "쀓발굴추적"


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


def db_now(session: Session) -> datetime:
    return session.execute(text("SELECT clock_timestamp()")).scalar_one()


class World:
    def __init__(self, session: Session, asof: datetime, ids: dict[str, int]) -> None:
        self.session = session
        self.asof = asof
        self.ids = ids


@pytest.fixture
def world(engine: object) -> Iterator[World]:
    """Four names, a history of articles for each, and the sweeps that read them.

    RISING: 2 articles over ten days read, then 6 in the last day.
    STEADY: 20 over ten days, then 3 — its usual rate.
    UNREAD: 6 in the last day, but no sweep recorded reading it.
    FOLLOWED: rising like RISING, but already tracked.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        ids: dict[str, int] = {}
        for n, name in enumerate((RISING, STEADY, UNREAD, FOLLOWED)):
            inst = Instrument(market=Market.KR, name=name, tracked=name == FOLLOWED)
            s.add(inst)
            s.flush()
            s.add(
                SymbolHistory(
                    instrument_id=inst.instrument_id,
                    symbol=f"99091{n}",
                    valid_from=datetime(2000, 1, 1, tzinfo=UTC).date(),
                    source="SEED",
                )
            )
            ids[name] = inst.instrument_id
        s.commit()

        now = db_now(s)
        stored_at = now - timedelta(minutes=5)
        plan = {
            RISING: [now - timedelta(days=d) for d in (3, 8)]
            + [now - timedelta(hours=h) for h in (1, 2, 3, 4, 5, 6)],
            STEADY: [now - timedelta(hours=12 * k + 30) for k in range(20)]
            + [now - timedelta(hours=h) for h in (2, 9, 17)],
            UNREAD: [now - timedelta(hours=h) for h in (1, 2, 3, 4, 5, 6)],
            FOLLOWED: [now - timedelta(days=d) for d in (3, 8)]
            + [now - timedelta(hours=h) for h in (1, 2, 3, 4, 5, 6)],
        }
        rows: list[QueryHitRow] = []
        for name, moments in plan.items():
            for k, at in enumerate(moments):
                item = NewsItem(
                    source=NewsSource.NAVER_NEWS,
                    url_hash=f"{name}-{k}-{HOST}".encode().hex()[:64].ljust(64, "0"),
                    url=f"https://{HOST}/{ids[name]}/{k}",
                    title=f"{name}, 기사 {k}",
                    summary="",
                    published_at=at,
                    available_at=at,
                    ingested_at=stored_at,
                )
                s.add(item)
                s.flush()
                rows.append(
                    QueryHitRow(
                        news_item_id=item.id,
                        instrument_id=ids[name],
                        matched_query=name,
                        decision=HitDecision.CONFIRMED,
                        decision_reason="test",
                        match_method=MatchMethod.NAME,
                        snippet="",
                        rule_version=RULE_VERSION,
                        decided_by=Decider.RULE,
                    )
                )
        news_repo.record_hits(s, rows)
        news_repo.record_coverage(
            s,
            [
                CoverageRow(
                    instrument_id=ids[name],
                    source=NewsSource.NAVER_NEWS,
                    collector="NAVER_NEWS",
                    covered_from=now - timedelta(days=11),
                    covered_to=now - timedelta(minutes=1),
                    capped=False,
                )
                for name in (RISING, STEADY, FOLLOWED)
            ],
        )
        s.commit()
        asof = db_now(s)
        try:
            yield World(s, asof, ids)
        finally:
            s.rollback()
            s.execute(text("DELETE FROM news_item WHERE url LIKE :h"), {"h": f"%{HOST}%"})
            for instrument_id in ids.values():
                for table in ("instrument_promotion", "news_sweep_coverage", "symbol_history"):
                    s.execute(
                        text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": instrument_id}
                    )
                s.execute(
                    text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": instrument_id}
                )
            s.commit()


def ours(world: World, asof: datetime | None = None) -> dict[str, discovery_service.Candidate]:
    found = discovery_service.discover(world.session, asof=asof or world.asof, top=100_000)
    by_id = {v: k for k, v in world.ids.items()}
    return {by_id[c.instrument_id]: c for c in found.candidates if c.instrument_id in by_id}


class TestWhatSurfaces:
    def test_a_surge_ranks_and_a_steady_rate_does_not_beat_it(self, world: World) -> None:
        found = ours(world)
        assert found[RISING].score > found[STEADY].score
        assert found[RISING].recent == 6
        assert found[RISING].baseline == 2
        assert found[STEADY].expected == pytest.approx(20 / found[STEADY].baseline_days, rel=0.01)

    def test_a_tracked_name_is_not_a_candidate(self, world: World) -> None:
        assert FOLLOWED not in ours(world)

    def test_mentions_nobody_recorded_reading_are_unmeasured(self, world: World) -> None:
        """Counted as unmeasured, never ranked as a surge against an empty past."""
        assert UNREAD not in ours(world)
        found = discovery_service.discover(world.session, asof=world.asof, top=100_000)
        assert found.unmeasured >= 1


class TestOnlyWhatWasReadIsCounted:
    def test_mentions_outside_the_read_stretches_do_not_count(self, world: World) -> None:
        """A busy name's pages cover a few hours each; the rest of the day was never read.

        UNREAD's six recent articles sit 1 to 6 hours back. Read here: the
        first four hours of the window and the last three and a half, plus
        two days of baseline. Only the three inside the read stretch count.
        """
        a = world.asof
        news_repo.record_coverage(
            world.session,
            [
                CoverageRow(
                    instrument_id=world.ids[UNREAD],
                    source=NewsSource.NAVER_NEWS,
                    collector="NAVER_NEWS",
                    covered_from=lo,
                    covered_to=hi,
                    capped=True,
                )
                for lo, hi in (
                    (a - timedelta(days=11), a - timedelta(days=9)),
                    (a - timedelta(hours=24), a - timedelta(hours=20)),
                    (a - timedelta(hours=3, minutes=30), a - timedelta(seconds=1)),
                )
            ],
        )
        world.session.commit()

        found = ours(world, db_now(world.session))
        assert found[UNREAD].recent == 3
        assert found[UNREAD].recent_days == pytest.approx(7.5 / 24, rel=0.01)


class TestOnlyTheRealCollectorsSweepsCount:
    def test_a_test_sweep_is_not_news_read(self, world: World) -> None:
        """Integration tests sweep the real master under their own name."""
        news_repo.record_coverage(
            world.session,
            [
                CoverageRow(
                    instrument_id=world.ids[UNREAD],
                    source=NewsSource.NAVER_NEWS,
                    collector="NAVER_NEWS_TEST",
                    covered_from=world.asof - timedelta(days=11),
                    covered_to=world.asof - timedelta(minutes=1),
                    capped=False,
                )
            ],
        )
        world.session.commit()

        assert UNREAD not in ours(world, db_now(world.session))

    def test_coverage_for_a_removed_instrument_is_skipped_not_fatal(self, world: World) -> None:
        missing = world.session.execute(
            text("SELECT coalesce(max(instrument_id), 0) + 1000 FROM instrument")
        ).scalar_one()
        news_repo.record_coverage(
            world.session,
            [
                CoverageRow(
                    instrument_id=missing,
                    source=NewsSource.NAVER_NEWS,
                    collector="NAVER_NEWS",
                    covered_from=world.asof - timedelta(days=1),
                    covered_to=world.asof,
                    capped=False,
                )
            ],
        )
        world.session.commit()

        assert (
            world.session.execute(
                text("SELECT count(*) FROM news_sweep_coverage WHERE instrument_id = :i"),
                {"i": missing},
            ).scalar_one()
            == 0
        )


class TestTheSameMomentGivesTheSameAnswer:
    def test_a_later_rejudgment_does_not_change_a_past_list(self, world: World) -> None:
        rows = [
            QueryHitRow(
                news_item_id=d.news_item_id,
                instrument_id=d.instrument_id,
                matched_query=RISING,
                decision=HitDecision.PENDING,
                decision_reason="test:later",
                match_method=MatchMethod.NAME,
                snippet="",
                rule_version=RULE_VERSION,
                decided_by=Decider.RULE,
            )
            for d in news_repo.decisions_asof(
                world.session, world.asof, instrument_ids=[world.ids[RISING]]
            )
        ]
        news_repo.record_hits(world.session, rows)
        world.session.commit()

        assert ours(world)[RISING].recent == 6
        assert RISING not in ours(world, db_now(world.session))

    def test_an_article_stored_later_is_invisible(self, world: World) -> None:
        world.session.execute(
            text("UPDATE news_item SET ingested_at = :t WHERE url LIKE :h"),
            {"t": world.asof + timedelta(hours=1), "h": f"https://{HOST}/{world.ids[RISING]}/%"},
        )
        world.session.commit()

        assert RISING not in ours(world)

    def test_a_sweep_recorded_later_had_not_been_read_then(self, world: World) -> None:
        news_repo.record_coverage(
            world.session,
            [
                CoverageRow(
                    instrument_id=world.ids[UNREAD],
                    source=NewsSource.NAVER_NEWS,
                    collector="NAVER_NEWS",
                    covered_from=world.asof - timedelta(days=11),
                    covered_to=world.asof - timedelta(minutes=1),
                    capped=False,
                )
            ],
        )
        world.session.commit()

        assert UNREAD not in ours(world)
        assert UNREAD in ours(world, db_now(world.session))

    def test_a_name_promoted_later_was_a_candidate_then(self, world: World) -> None:
        promotion_repo.promote(
            world.session,
            promotion_repo.PromotionRow(
                instrument_id=world.ids[STEADY],
                discovered_asof=world.asof,
                window_hours=24,
                baseline_days=10.0,
                recent_mentions=3,
                baseline_mentions=20,
                score=1.0,
                news_freshness="FRESH",
                candle_bars=1,
                fundamental_facts=0,
            ),
        )
        world.session.commit()

        assert STEADY in ours(world)
        assert STEADY not in ours(world, db_now(world.session))


class TestWhetherTheNewsWasFlowing:
    def test_a_list_made_long_after_the_last_article_says_so(self, world: World) -> None:
        found = discovery_service.discover(world.session, asof=world.asof + timedelta(days=10))
        assert found.freshness is Freshness.STALE

    def test_an_article_published_after_the_moment_is_not_the_newest_then(
        self, world: World
    ) -> None:
        """Stored in a sweep that began before `asof`, published after it."""
        later = world.asof + timedelta(hours=3)
        world.session.add(
            NewsItem(
                source=NewsSource.NAVER_NEWS,
                url_hash=f"late-{HOST}".encode().hex()[:64].ljust(64, "0"),
                url=f"https://{HOST}/late",
                title="late",
                summary="",
                published_at=later,
                available_at=later,
                ingested_at=world.asof - timedelta(minutes=1),
            )
        )
        world.session.commit()

        newest = news_repo.latest_available_at(
            world.session, source=NewsSource.NAVER_NEWS, ingested_before=world.asof
        )
        assert newest is not None and newest <= world.asof

    def test_a_list_made_while_it_flows_is_fresh(self, world: World) -> None:
        found = discovery_service.discover(world.session, asof=world.asof)
        assert found.freshness is Freshness.FRESH


class TestPromotion:
    def _fakes(
        self, monkeypatch: pytest.MonkeyPatch, bars: dict[int, int]
    ) -> tuple[Any, Any, list[list[int]]]:
        asked: list[list[int]] = []

        def prices(session: Session, ids: Any) -> CollectorStatus:
            asked.append(list(ids))
            return CollectorStatus.SUCCESS

        def fundamentals(session: Session, ids: Any) -> CollectorStatus:
            return CollectorStatus.SUCCESS

        monkeypatch.setattr(
            promotion_service.candle_repo, "count_for", lambda s, i, interval: bars.get(i, 0)
        )
        monkeypatch.setattr(promotion_service.fundamental_repo, "count_for", lambda s, i: 7)
        return prices, fundamentals, asked

    def test_data_first_then_tracked_with_the_evidence(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        found = discovery_service.discover(world.session, asof=world.asof, top=100_000)
        rising = [c for c in found.candidates if c.instrument_id == world.ids[RISING]]
        prices, fundamentals, asked = self._fakes(monkeypatch, {world.ids[RISING]: 1200})

        (outcome,) = promotion_service.promote(
            world.session, found, rising, prices=prices, fundamentals=fundamentals
        )

        assert asked == [[world.ids[RISING]]]
        assert outcome.promoted
        world.session.expire_all()
        assert world.session.get(Instrument, world.ids[RISING]).tracked  # type: ignore[union-attr]
        (row,) = world.session.execute(
            select(InstrumentPromotion).where(
                InstrumentPromotion.instrument_id == world.ids[RISING]
            )
        ).scalars()
        assert (row.recent_mentions, row.baseline_mentions, row.candle_bars) == (6, 2, 1200)
        assert row.fundamental_facts == 7
        assert row.news_freshness == "FRESH"
        assert row.discovered_asof == world.asof

    def test_no_prices_no_promotion(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        found = discovery_service.discover(world.session, asof=world.asof, top=100_000)
        rising = [c for c in found.candidates if c.instrument_id == world.ids[RISING]]
        prices, fundamentals, _ = self._fakes(monkeypatch, {})

        (outcome,) = promotion_service.promote(
            world.session, found, rising, prices=prices, fundamentals=fundamentals
        )

        assert not outcome.promoted
        world.session.expire_all()
        assert not world.session.get(Instrument, world.ids[RISING]).tracked  # type: ignore[union-attr]
        assert promotion_repo.history(world.session, world.ids[RISING]) == []

    def test_a_tracked_name_cannot_be_promoted_again(self, world: World) -> None:
        with pytest.raises(ValueError, match="already tracked"):
            promotion_repo.promote(
                world.session,
                promotion_repo.PromotionRow(
                    instrument_id=world.ids[FOLLOWED],
                    discovered_asof=world.asof,
                    window_hours=24,
                    baseline_days=10.0,
                    recent_mentions=6,
                    baseline_mentions=2,
                    score=5.0,
                    news_freshness="FRESH",
                    candle_bars=1,
                    fundamental_facts=0,
                ),
            )
