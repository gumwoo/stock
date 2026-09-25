"""The overlay against the database: what could be known at a moment, and nothing else.

Pinned: a reading made after the moment, an article stored after it and a
relevance verdict withdrawn after it all leave the overlay at that moment
unchanged; readings of another model or prompt are never mixed in; unread
confirmed articles are counted; a market with no news feed says so; the
overlay is filed beside a signal without touching its score, and its failure
never costs the signal.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.naver_news import RULE_VERSION
from app.config import get_settings
from app.core.calendar import Market
from app.core.types import Freshness, MissingFactorPolicy, SignalAction
from app.models import Base, Instrument, Signal, SignalOverlay, SymbolHistory
from app.models.news import Decider, HitDecision, MatchMethod, NewsItem, NewsSource, SentimentEvent
from app.repositories import llm_repo, news_repo
from app.repositories.llm_repo import SentimentRow
from app.repositories.news_repo import QueryHitRow
from app.services import overlay_service, scoring_service
from app.services.llm_service import SENTIMENT_PROMPT_VERSION

pytestmark = pytest.mark.integration

HOST = "overlay-fixture.example.com"
NAME = "쀓오버"
MODEL = "fake-reader"


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
    def __init__(self, session: Session, instrument: Instrument, items: list[int]) -> None:
        self.session = session
        self.instrument = instrument
        self.items = items
        self.id = instrument.instrument_id


def hit(item: int, instrument_id: int, decision: HitDecision, by: Decider) -> QueryHitRow:
    return QueryHitRow(
        news_item_id=item,
        instrument_id=instrument_id,
        matched_query=NAME,
        decision=decision,
        decision_reason="test",
        match_method=MatchMethod.NAME,
        snippet="s",
        rule_version=RULE_VERSION,
        decided_by=by,
    )


def read(
    item: int, instrument_id: int, *, model: str = MODEL, sentiment: float = 0.8
) -> SentimentRow:
    return SentimentRow(
        news_item_id=item,
        instrument_id=instrument_id,
        model=model,
        prompt_version=SENTIMENT_PROMPT_VERSION,
        sentiment=sentiment,
        event_type=SentimentEvent.SHAREHOLDER_RETURN,
        intensity=0.6,
        confidence=0.9,
        evidence="자사주 매입",
    )


@pytest.fixture
def world(engine: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """Four confirmed articles about one buyback, three of them read."""
    monkeypatch.setattr(get_settings(), "sentiment_llm_model", MODEL)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.KR, name=NAME, tracked=False)
        s.add(inst)
        s.flush()
        s.add(
            SymbolHistory(
                instrument_id=inst.instrument_id,
                symbol="990977",
                valid_from=datetime(2000, 1, 1, tzinfo=UTC).date(),
                source="SEED",
            )
        )
        now = db_now(s)
        items = []
        for n in range(4):
            at = now - timedelta(hours=3) + timedelta(minutes=10 * n)
            item = NewsItem(
                source=NewsSource.NAVER_NEWS,
                url_hash=f"{HOST}-{n}".encode().hex()[:64].ljust(64, "0"),
                url=f"https://{HOST}/{n}",
                title=f"{NAME}, 자사주 매입 {n}",
                summary=None,
                published_at=at,
                available_at=at,
                ingested_at=now - timedelta(hours=1),
            )
            s.add(item)
            s.flush()
            items.append(item.id)
        news_repo.record_hits(
            s, [hit(i, inst.instrument_id, HitDecision.CONFIRMED, Decider.RULE) for i in items]
        )
        llm_repo.save_readings(s, [read(i, inst.instrument_id) for i in items[:3]])
        s.commit()
        try:
            yield World(s, inst, items)
        finally:
            s.rollback()
            s.execute(
                text("DELETE FROM signal WHERE instrument_id = :i"), {"i": inst.instrument_id}
            )
            s.execute(
                text("DELETE FROM disclosure WHERE instrument_id = :i"), {"i": inst.instrument_id}
            )
            s.execute(text("DELETE FROM news_item WHERE url LIKE :h"), {"h": f"%{HOST}%"})
            s.execute(
                text("DELETE FROM symbol_history WHERE instrument_id = :i"),
                {"i": inst.instrument_id},
            )
            s.execute(
                text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": inst.instrument_id}
            )
            s.commit()


def at(world: World, moment: datetime | None = None) -> overlay_service.OverlayAt:
    moment = moment or db_now(world.session)
    return overlay_service.overlays_at(world.session, asof=moment, instrument_ids=[world.id])[
        world.id
    ]


class TestWhatCounts:
    def test_one_buyback_read_three_times_is_one_event(self, world: World) -> None:
        result = at(world)
        assert len(result.overlay.clusters) == 1
        assert result.overlay.clusters[0].articles == 3
        assert result.overlay.points > 0
        assert result.unread_articles == 1

    def test_another_models_readings_are_not_mixed_in(self, world: World) -> None:
        llm_repo.save_readings(
            world.session, [read(world.items[3], world.id, model="other-model", sentiment=-1.0)]
        )
        world.session.commit()
        result = at(world)
        assert result.overlay.readings_used == 3
        assert result.unread_articles == 1

    def test_a_korean_name_reads_the_news_feed_and_others_have_none(self, world: World) -> None:
        assert at(world).news_freshness is Freshness.FRESH
        world.instrument.market = Market.US
        world.session.commit()
        assert at(world).news_freshness is Freshness.MISSING


class TestTheSameMomentGivesTheSameAnswer:
    def test_a_reading_made_later_is_invisible(self, world: World) -> None:
        before = db_now(world.session)
        llm_repo.save_readings(world.session, [read(world.items[3], world.id)])
        world.session.commit()

        assert at(world, before).overlay.readings_used == 3
        assert at(world).overlay.readings_used == 4

    def test_a_verdict_withdrawn_later_still_counted_then(self, world: World) -> None:
        """The verification's point: a reading must not outlive its verdict."""
        before = db_now(world.session)
        news_repo.record_hits(
            world.session, [hit(world.items[0], world.id, HitDecision.REJECTED, Decider.LLM)]
        )
        world.session.commit()

        assert at(world, before).overlay.readings_used == 3
        assert at(world).overlay.readings_used == 2

    def test_an_article_stored_later_is_invisible(self, world: World) -> None:
        moment = db_now(world.session)
        world.session.execute(
            text("UPDATE news_item SET ingested_at = :t WHERE url = :u"),
            {"t": moment + timedelta(hours=1), "u": f"https://{HOST}/0"},
        )
        world.session.commit()
        assert at(world, moment).overlay.readings_used == 2


class TestDisclosures:
    def file(self, world: World, *, title: str, available: datetime, stored: datetime) -> None:
        world.session.execute(
            text(
                "INSERT INTO disclosure (instrument_id, rcept_no, report_nm, pblntf_ty, filer, "
                "filed_on, available_at, ingested_at) VALUES (:i, :r, :t, 'B', NULL, :f, :a, :s)"
            ),
            {
                "i": world.id,
                "r": f"2099{world.id:010d}"[:14],
                "t": title,
                "f": available.date(),
                "a": available,
                "s": stored,
            },
        )
        world.session.commit()

    def test_a_buyback_filing_joins_the_buyback_articles(self, world: World) -> None:
        now = db_now(world.session)
        self.file(
            world,
            title="주요사항보고서(자기주식취득결정)",
            available=now - timedelta(hours=2),
            stored=now - timedelta(hours=2),
        )
        result = at(world)
        (cluster,) = result.overlay.clusters
        assert (cluster.articles, len(cluster.disclosure_ids)) == (4, 1)

    def test_a_filing_not_yet_available_or_not_yet_stored_is_not_there(self, world: World) -> None:
        now = db_now(world.session)
        self.file(
            world,
            title="주요사항보고서(유상증자결정)",
            available=now + timedelta(hours=10),
            stored=now - timedelta(hours=1),
        )
        assert all(c.event_type != "CAPITAL_RAISE" for c in at(world).overlay.clusters)
        assert (
            all(
                c.event_type != "CAPITAL_RAISE"
                for c in at(world, now + timedelta(hours=11)).overlay.clusters
            )
            is False
        )

    def test_a_filing_stored_after_the_moment_is_invisible(self, world: World) -> None:
        now = db_now(world.session)
        self.file(
            world,
            title="주요사항보고서(유상증자결정)",
            available=now - timedelta(hours=1),
            stored=now + timedelta(hours=1),
        )
        assert all(c.event_type != "CAPITAL_RAISE" for c in at(world, now).overlay.clusters)


def signal_row(world: World) -> Signal:
    now = db_now(world.session)
    row = Signal(
        instrument_id=world.id,
        data_asof=now,
        decision_at=now,
        earliest_execution_at=now + timedelta(hours=18),
        total_score=55.0,
        action=SignalAction.WATCH,
        policy=MissingFactorPolicy.ZERO,
        reasons=[],
        strategy_version="test",
    )
    world.session.add(row)
    world.session.commit()
    return row


class TestBesideTheSignal:
    def test_the_overlay_is_filed_and_the_score_is_untouched(self, world: World) -> None:
        row = signal_row(world)
        overlay = overlay_service.attach(world.session, row)

        assert overlay is not None and overlay.points > 0
        world.session.expire_all()
        stored = world.session.get(Signal, row.id)
        assert stored is not None
        assert (stored.total_score, stored.action) == (55.0, SignalAction.WATCH)
        assert overlay.detail[0]["articles"] == 3
        assert (overlay.reading_model, overlay.unread_articles) == (MODEL, 1)

    def test_a_failing_overlay_costs_the_signal_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = signal_row(world)

        def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("overlay broke")

        monkeypatch.setattr(overlay_service, "overlays_at", broken)
        assert overlay_service.attach(world.session, row) is None
        world.session.expire_all()
        assert world.session.get(Signal, row.id) is not None

    def test_scoring_files_an_overlay_for_every_signal(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wiring only: the scorer itself is exercised elsewhere."""
        row = signal_row(world)
        monkeypatch.setattr(
            scoring_service.instrument_repo, "list_active", lambda *a, **k: [world.instrument]
        )
        monkeypatch.setattr(scoring_service, "market_peer_lookup", lambda *a, **k: None)
        scored = SimpleNamespace(action=SignalAction.WATCH, total_score=55.0)
        monkeypatch.setattr(scoring_service, "score_instrument", lambda *a, **k: scored)
        monkeypatch.setattr(scoring_service, "persist_signal", lambda *a, **k: row)

        scoring_service.score_all(world.session)

        found = world.session.execute(
            select(SignalOverlay).where(SignalOverlay.signal_id == row.id)
        ).scalar_one()
        assert found.events == 1
