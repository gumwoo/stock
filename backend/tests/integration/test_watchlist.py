"""The morning watchlist, taken on a crafted morning: who is in, why, and what is frozen.

The morning is 2025-06-02 08:50 in Seoul, before any real snapshot existed,
so nothing real is on that date; the snapshot and every row made here are
removed afterwards. The pool is narrowed to the names made here, so other
tests' temporary names — and the real master — stay out of it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.types import Availability, Engine, Freshness, MissingFactorPolicy, SignalAction
from app.models import Base, Disclosure, Instrument, Signal, SignalFactor
from app.models.promotion import InstrumentPromotion
from app.models.watchlist import WatchlistMember, WatchlistSnapshot
from app.scoring.policy import STRATEGY_VERSION as SIGNAL_STRATEGY
from app.services import watchlist_service

pytestmark = pytest.mark.integration

KR = MarketCalendar(Market.KR)
DAY = date(2025, 6, 2)
ASOF = datetime(2025, 6, 1, 23, 50, tzinfo=UTC)  # 08:50 in Seoul on DAY
PREVIOUS = date(2025, 5, 30)


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


class World:
    def __init__(self, session: Session, names: dict[str, int]) -> None:
        self.session = session
        self.names = names


@pytest.fixture
def world(engine: object) -> Iterator[World]:
    assert (
        KR.is_session(DAY)
        and KR.sessions_between(date(2025, 5, 26), date(2025, 6, 1))[-1] == PREVIOUS
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        names = {}
        for key, tracked in (("scored", True), ("filed", False), ("late", True), ("quiet", False)):
            inst = Instrument(market=Market.KR, name=f"쀓관찰{key}", tracked=tracked)
            s.add(inst)
            s.flush()
            names[key] = inst.instrument_id
        # A tracked name whose last close said BUY_INTEREST.
        decided = KR.session_close(PREVIOUS)
        signal = Signal(
            instrument_id=names["scored"],
            data_asof=decided,
            decision_at=decided,
            earliest_execution_at=KR.session_open(DAY),
            total_score=71.4,
            action=SignalAction.BUY_INTEREST,
            policy=MissingFactorPolicy.ZERO,
            reasons=[],
            strategy_version=SIGNAL_STRATEGY,
            ingested_at=decided,
        )
        s.add(signal)
        # The same name judged again at the day's own close: after the moment.
        s.add(
            Signal(
                instrument_id=names["scored"],
                data_asof=KR.session_close(DAY),
                decision_at=KR.session_close(DAY),
                earliest_execution_at=KR.next_session_open(DAY),
                total_score=20.0,
                action=SignalAction.CAUTION,
                policy=MissingFactorPolicy.ZERO,
                reasons=[],
                strategy_version=SIGNAL_STRATEGY,
                ingested_at=KR.session_close(DAY),
            )
        )
        s.flush()
        for engine_name, score in ((Engine.TECHNICAL, 80.0), (Engine.FUNDAMENTAL, 63.0)):
            s.add(
                SignalFactor(
                    signal_id=signal.id,
                    engine=engine_name,
                    score=score,
                    metrics=[],
                    requested_weight=0.5,
                    effective_weight=0.5,
                    contribution=score / 2,
                    availability=Availability.AVAILABLE,
                    freshness_status=Freshness.FRESH,
                )
            )
        # A buyback filed on the previous session, stored the evening after; one
        # filed on the day itself, and one stored only after the moment.
        for rcept, filed_on, stored in (
            ("20000104999701", PREVIOUS, ASOF - timedelta(hours=12)),
            ("20000104999702", DAY, ASOF - timedelta(hours=1)),
            ("20000104999703", PREVIOUS, ASOF + timedelta(minutes=5)),
        ):
            s.add(
                Disclosure(
                    instrument_id=names["filed"] if rcept.endswith("1") else names["quiet"],
                    rcept_no=rcept,
                    report_nm="주요사항보고서(자기주식취득결정)",
                    pblntf_ty="B",
                    filer="테스트",
                    filed_on=filed_on,
                    available_at=KR.next_session_open(filed_on),
                    ingested_at=stored,
                )
            )
        # Tracked now, but promoted only after the moment: untracked then.
        s.add(
            InstrumentPromotion(
                instrument_id=names["late"],
                promoted_at=ASOF + timedelta(days=3),
                discovered_asof=ASOF + timedelta(days=3),
                window_hours=24,
                baseline_days=14.0,
                recent_mentions=5,
                baseline_mentions=1,
                score=3.0,
                news_freshness="FRESH",
                candle_bars=100,
                fundamental_facts=0,
            )
        )
        s.commit()
        try:
            yield World(s, names)
        finally:
            s.rollback()
            s.execute(text("DELETE FROM watchlist_snapshot WHERE session_date = :d"), {"d": DAY})
            s.execute(text("DELETE FROM disclosure WHERE rcept_no LIKE '2000010499970%'"))
            for i in names.values():
                for table in ("signal", "instrument_promotion"):
                    s.execute(text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": i})
                s.execute(text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": i})
            s.commit()


def members(world: World, snapshot_id: int) -> dict[int, WatchlistMember]:
    world.session.expire_all()
    return {
        m.instrument_id: m
        for m in world.session.execute(
            select(WatchlistMember).where(WatchlistMember.snapshot_id == snapshot_id)
        ).scalars()
    }


def test_the_morning_is_chosen_and_frozen_with_its_reasons(world: World) -> None:
    snap = watchlist_service.take_snapshot(world.session, now=ASOF, only=world.names.values())
    assert snap is not None
    assert (snap.session_date, snap.strategy_version) == (DAY, "PREOPEN_V1")
    got = members(world, snap.id)

    scored = got[world.names["scored"]]
    assert scored.reasons == ["TRACKED_HIGH_SCORE", "TRACKED"]
    assert (scored.total_score, scored.technical_score, scored.fundamental_score) == (
        71.4,
        80.0,
        63.0,
    )
    assert scored.last_action == "BUY_INTEREST"
    assert scored.signal_decision_at == KR.session_close(PREVIOUS)

    # Filed the session before and stored by the moment: known that morning.
    assert got[world.names["filed"]].reasons == ["DISCLOSURE_EVENT"]
    # Filed the same day, or stored after the moment: not.
    assert world.names["quiet"] not in got
    # Promoted after the moment: not tracked then, so not on the list for it.
    assert world.names["late"] not in got

    ranks = sorted(m.rank for m in got.values())
    assert ranks == sorted(set(ranks))
    assert set(snap.inputs) == {"news", "llm", "search_trends", "disclosures"}
    assert {"overlay", "relevance_rule", "sentiment_prompt", "attention", "regime"} <= set(
        snap.versions
    )


def test_one_snapshot_a_morning(world: World) -> None:
    assert (
        watchlist_service.take_snapshot(world.session, now=ASOF, only=world.names.values())
        is not None
    )
    assert (
        watchlist_service.take_snapshot(
            world.session, now=ASOF + timedelta(minutes=3), only=world.names.values()
        )
        is None
    )
    count = world.session.execute(
        select(WatchlistSnapshot).where(WatchlistSnapshot.session_date == DAY)
    ).all()
    assert len(count) == 1


def test_no_list_on_a_day_without_a_session(world: World) -> None:
    sunday = datetime(2025, 6, 1, 0, 0, tzinfo=UTC)
    assert (
        watchlist_service.take_snapshot(world.session, now=sunday, only=world.names.values())
        is None
    )


def test_no_list_once_the_session_has_opened(world: World) -> None:
    after_open = ASOF + timedelta(minutes=20)  # 09:10 in Seoul
    assert (
        watchlist_service.take_snapshot(world.session, now=after_open, only=world.names.values())
        is None
    )


def test_the_database_refuses_a_second_list_for_a_morning(world: World) -> None:
    from sqlalchemy.exc import IntegrityError

    snap = watchlist_service.take_snapshot(world.session, now=ASOF, only=world.names.values())
    assert snap is not None
    world.session.add(
        WatchlistSnapshot(
            session_date=DAY,
            asof=ASOF,
            strategy_version=snap.strategy_version,
            selection_version=1,
            versions={},
            inputs={},
            pool=0,
            left_out=0,
        )
    )
    with pytest.raises(IntegrityError):
        world.session.flush()
    world.session.rollback()
