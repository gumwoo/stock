"""PREOPEN_V2 아침 흐름을 꾸민 아침에 돌려 본다: 풀, 사전 수집, 점수, 보충, 목록, 실시간 목록.

아침은 2025-06-04 08:50(서울)이다. 실제 풀이나 목록이 있을 수 없는 날이고, 여기서
만든 풀·목록·공시·일봉·종목은 끝나면 지운다. 풀은 여기서 만든 종목으로 좁힌다
(`only`). 네트워크와 LLM은 부르지 않는다. 수집기와 LLM 실행은 가짜로 바꾼다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.models import Base, Candle, CollectorRun, Disclosure, Instrument, Interval, Signal
from app.models.collector import CollectorStatus
from app.models.preopen import DEGRADED_FALLBACK, NORMAL, PreopenPool, PreopenPoolMember
from app.models.watchlist import WatchlistMember, WatchlistSnapshot
from app.realtime import gateway
from app.services import llm_service, preopen_service

pytestmark = pytest.mark.integration

SEOUL = ZoneInfo("Asia/Seoul")
KR = MarketCalendar(Market.KR)
DAY = date(2025, 6, 4)
PREVIOUS = date(2025, 6, 2)  # 6/3은 대통령 선거일로 휴장
ASOF = datetime.combine(DAY, time(8, 50), tzinfo=SEOUL)
MORNING = datetime.combine(DAY, time(7, 0), tzinfo=SEOUL)
RCEPT = "2000010599970"


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

    @property
    def ids(self) -> list[int]:
        return sorted(self.names.values())


def _bar(instrument_id: int, day: date) -> Candle:
    return Candle(
        instrument_id=instrument_id,
        interval=Interval.DAY_1,
        ts=KR.session_open(day),
        available_at=KR.session_close(day),
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(90),
        close=Decimal(105),
        volume=Decimal(1000),
        source="TEST",
    )


@pytest.fixture
def world(engine: object) -> Iterator[World]:
    assert KR.is_session(DAY) and not KR.is_session(date(2025, 6, 3))
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        names: dict[str, int] = {}
        # 약한 공시 종목을 먼저 만든다. 종목 id 순서가 공시 강도 순서와 반대여야
        # "강한 공시 먼저"가 id 동점 처리와 구별된다.
        for key, tracked in (("quiet", True), ("weak", False), ("strong", False)):
            inst = Instrument(market=Market.KR, name=f"쀓장전{key}", tracked=tracked)
            s.add(inst)
            s.flush()
            names[key] = inst.instrument_id
        # 자사주 취득(강도 0.6)과 현금배당(0.3): 둘 다 사건 공시, 전 거래일 제출.
        for n, (key, title) in enumerate(
            (("strong", "주요사항보고서(자기주식취득결정)"), ("weak", "현금배당결정"))
        ):
            s.add(
                Disclosure(
                    instrument_id=names[key],
                    rcept_no=f"{RCEPT}{n}",
                    report_nm=title,
                    pblntf_ty="B",
                    filer="테스트",
                    filed_on=PREVIOUS,
                    available_at=KR.next_session_open(PREVIOUS),
                    ingested_at=ASOF - timedelta(hours=12),
                )
            )
        s.commit()
        try:
            yield World(s, names)
        finally:
            s.rollback()
            s.execute(text("DELETE FROM watchlist_snapshot WHERE session_date = :d"), {"d": DAY})
            s.execute(text("DELETE FROM preopen_pool WHERE session_date = :d"), {"d": DAY})
            s.execute(text("DELETE FROM disclosure WHERE rcept_no LIKE :r"), {"r": f"{RCEPT}%"})
            for i in names.values():
                for table in ("candle", "signal"):
                    s.execute(text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": i})
                s.execute(text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": i})
            s.commit()


def _pool(world: World, *, frozen_at: datetime | None = MORNING) -> PreopenPool:
    pool = PreopenPool(
        session_date=DAY,
        status=NORMAL,
        started_at=MORNING,
        discovery={},
        stages={},
        pool_count=0,
    )
    world.session.add(pool)
    world.session.commit()
    if frozen_at is not None:
        preopen_service.freeze(world.session, pool, asof=frozen_at, only=world.ids)
    return pool


def _members(world: World, pool: PreopenPool) -> dict[int, PreopenPoolMember]:
    world.session.expire_all()
    return {m.instrument_id: m for m in preopen_service.members_of(world.session, pool)}


def _list(world: World, snapshot_id: int) -> list[WatchlistMember]:
    world.session.expire_all()
    return list(
        world.session.execute(
            select(WatchlistMember)
            .where(WatchlistMember.snapshot_id == snapshot_id)
            .order_by(WatchlistMember.rank)
        ).scalars()
    )


class TestPoolAndList:
    def test_the_pool_is_frozen_with_where_each_name_came_from(self, world: World) -> None:
        pool = _pool(world)
        got = _members(world, pool)
        n = world.names
        assert set(got) == set(world.ids)
        assert got[n["quiet"]].sources == ["FOCUS"] and got[n["quiet"]].tracked
        assert got[n["strong"]].sources == ["DISCLOSURE"]
        assert got[n["strong"]].disclosure_intensity == pytest.approx(0.6)
        assert got[n["weak"]].disclosure_intensity == pytest.approx(0.3)
        world.session.refresh(pool)
        assert pool.asof == MORNING and pool.pool_count == 3
        assert pool.discovery["top"] == preopen_service.DISCOVERY_TOP == 30
        # 이미 얼린 풀은 다시 얼리지 않는다.
        status, _ = preopen_service.freeze(world.session, pool, asof=ASOF, only=world.ids)
        assert status == preopen_service.SKIPPED

    def test_only_names_with_an_event_are_listed_strongest_first(self, world: World) -> None:
        pool = _pool(world)
        snap = preopen_service.take_snapshot(world.session, now=ASOF, only=world.ids)
        assert snap is not None
        assert (snap.strategy_version, snap.selection_version) == ("PREOPEN_V2", 2)
        assert snap.pool_id == pool.id and snap.pool == 3
        rows = _list(world, snap.id)
        # 추적 중이지만 오늘 사건이 없는 종목은 없다. 공시 강도가 순서를 정한다.
        assert [r.instrument_id for r in rows] == [world.names["strong"], world.names["weak"]]
        assert all(r.reasons == ["DISCLOSURE_EVENT"] for r in rows)
        assert all(r.score_source == "PREOPEN" and r.signal_decision_at is None for r in rows)
        assert {"news", "llm", "search_trends", "disclosures", "pool", "prefetch"} <= set(
            snap.inputs
        )
        assert snap.inputs["pool"]["status"] == NORMAL  # type: ignore[index]
        world.session.refresh(pool)
        assert pool.stages["snapshot"]["status"] == "SUCCESS"  # type: ignore[index]
        # 하루 하나.
        assert preopen_service.take_snapshot(world.session, now=ASOF, only=world.ids) is None

    def test_a_morning_without_a_pool_builds_one_as_a_fallback(self, world: World) -> None:
        snap = preopen_service.take_snapshot(world.session, now=ASOF, only=world.ids)
        assert snap is not None
        pool = preopen_service.pool_for(world.session, DAY)
        assert pool is not None and pool.status == DEGRADED_FALLBACK
        assert snap.pool_id == pool.id and pool.asof == ASOF
        assert pool.stages["pool"]["status"] == "SUCCESS"  # type: ignore[index]
        assert snap.inputs["pool"]["status"] == DEGRADED_FALLBACK  # type: ignore[index]
        assert len(_list(world, snap.id)) == 2

    def test_no_list_after_the_open(self, world: World) -> None:
        after = datetime.combine(DAY, time(9, 1), tzinfo=SEOUL)
        assert preopen_service.take_snapshot(world.session, now=after, only=world.ids) is None

    def test_an_empty_list_stays_empty_on_the_live_page(self, world: World) -> None:
        _pool(world)
        quiet = [world.names["quiet"]]
        # 같은 날 V1 목록이 먼저 있어도 V2를 고르고, 예외가 나지 않는다. V1을 먼저
        # 넣어 두어야 정렬 없이 첫 행을 고르는 코드가 이 테스트를 통과하지 못한다.
        v1 = WatchlistSnapshot(
            session_date=DAY,
            asof=ASOF,
            strategy_version="PREOPEN_V1",
            selection_version=1,
            versions={},
            inputs={},
            pool=1,
            left_out=0,
        )
        world.session.add(v1)
        world.session.flush()
        world.session.add(
            WatchlistMember(
                snapshot_id=v1.id,
                instrument_id=world.names["quiet"],
                rank=1,
                reasons=["TRACKED"],
                tracked=True,
                overlay_events=[],
            )
        )
        world.session.commit()
        snap = preopen_service.take_snapshot(world.session, now=ASOF, only=quiet)
        assert snap is not None and _list(world, snap.id) == []
        source, members = gateway.load_members(DAY)
        assert (source, members) == (gateway.MORNING_LIST_EMPTY, [])

    def test_a_pool_that_cannot_be_frozen_gives_no_list_rather_than_an_empty_one(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 0개 목록이면 화면이 장애를 "오늘 조건에 맞는 종목 없음"으로 보여 준다.
        def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("discovery down")

        monkeypatch.setattr(preopen_service, "freeze", broken)
        assert preopen_service.take_snapshot(world.session, now=ASOF, only=world.ids) is None
        pool = preopen_service.pool_for(world.session, DAY)
        assert pool is not None and pool.status == DEGRADED_FALLBACK
        assert pool.stages["pool"]["status"] == "FAILED"  # type: ignore[index]
        count = world.session.execute(
            select(func.count())
            .select_from(WatchlistSnapshot)
            .where(WatchlistSnapshot.session_date == DAY)
        ).scalar()
        assert count == 0
        source, _ = gateway.load_members(DAY)
        assert source == gateway.TRACKED_FALLBACK


class TestPrefetch:
    def test_stale_names_are_fetched_in_rank_order_up_to_the_cap(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = world.session
        n = world.names
        strong = s.get(Instrument, n["strong"])
        assert strong is not None
        strong.kr_corp_code = "99999970"
        s.add(_bar(n["quiet"], PREVIOUS))  # 추적 종목은 직전 종가까지 있다
        s.commit()
        pool = _pool(world)
        now = datetime.combine(DAY, time(7, 15), tzinfo=SEOUL)
        fetched_at = now + timedelta(minutes=1)
        calls: list[tuple[str, list[int], object]] = []

        def prices(session: Session, ids: Any, *, period: str) -> CollectorRun:
            calls.append(("prices", list(ids), period))
            for i in ids:
                session.add(_bar(i, PREVIOUS))
            session.commit()
            return CollectorRun(
                source="PREFETCH_TEST", started_at=now, status=CollectorStatus.SUCCESS
            )

        def fundamentals(session: Session, ids: Any, *, years_back: int) -> CollectorRun:
            calls.append(("fundamentals", list(ids), years_back))
            return CollectorRun(
                source="PREFETCH_TEST",
                started_at=now,
                finished_at=fetched_at,
                status=CollectorStatus.SUCCESS,
            )

        status, detail = preopen_service.prefetch(
            s, pool, now=now, cap=1, prices=prices, fundamentals=fundamentals
        )
        got = _members(world, pool)
        # 임시 순위: 강한 공시 1위, 약한 공시 2위, 이유 없는 추적 종목은 순위 없음.
        assert got[n["strong"]].provisional_rank == 1 and got[n["weak"]].provisional_rank == 2
        assert got[n["quiet"]].provisional_rank is None
        assert calls == [
            ("prices", [n["strong"]], preopen_service.NEW_PRICES_PERIOD),
            ("fundamentals", [n["strong"]], preopen_service.NEW_FUNDAMENTAL_YEARS),
        ]
        assert got[n["strong"]].prefetch_status == preopen_service.FETCHED
        assert got[n["strong"]].fundamental_checked_at == fetched_at
        assert got[n["weak"]].prefetch_status == preopen_service.SKIPPED_CAP
        assert got[n["quiet"]].prefetch_status == preopen_service.FRESH
        assert status == preopen_service.PARTIAL and "SKIPPED_CAP 1" in detail
        # 추적 등록은 하지 않는다.
        s.expire_all()
        assert not s.get(Instrument, n["strong"]).tracked  # type: ignore[union-attr]
        # 다음 아침은 7일 안이면 재무를 다시 받지 않는다.
        assert preopen_service.last_fundamental_check(s, n["strong"]) == fetched_at

    def test_prefetch_runs_are_not_named_like_the_sources_they_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `last_success("DART")`는 이름 앞부분으로 찾는다. 몇 종목짜리 실행이
        # 추적 종목 전체의 재무 신선도를 속이면 안 된다.
        names: list[str] = []
        monkeypatch.setattr(
            preopen_service, "run_collector", lambda c, _s: names.append(c.name) or c
        )
        preopen_service.fetch_fundamentals(None, [1], years_back=2)  # type: ignore[arg-type]
        preopen_service.fetch_prices(None, [1], period="1mo")  # type: ignore[arg-type]
        assert all(name.startswith("PREFETCH_") for name in names)
        assert not any(name.startswith(("DART", "YFINANCE")) for name in names)


class TestScores:
    def test_scores_are_kept_on_the_pool_and_never_in_signal(self, world: World) -> None:
        pool = _pool(world)
        before = world.session.execute(
            select(func.count()).select_from(Signal).where(Signal.instrument_id.in_(world.ids))
        ).scalar()
        now = datetime.combine(DAY, time(8, 40), tzinfo=SEOUL)
        status, _ = preopen_service.score_pool(world.session, pool, now=now)
        assert status == preopen_service.SUCCESS
        for m in _members(world, pool).values():
            assert m.evaluated_at == now
            assert m.peer_count is not None and m.peer_count >= 1
            assert m.peer_hash is not None and len(m.peer_hash) == 64
            assert m.total_score is None and m.abstained_reason == "일봉이 없어 점수를 낼 수 없음"
        after = world.session.execute(
            select(func.count()).select_from(Signal).where(Signal.instrument_id.in_(world.ids))
        ).scalar()
        assert before == after == 0


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class TestStages:
    def test_the_supplement_waits_for_the_morning_reading_and_gives_up_at_the_deadline(
        self, world: World
    ) -> None:
        pool = _pool(world)
        preopen_service._mark(world.session, pool, "llm", "RUNNING")
        clock = Clock(datetime.combine(DAY, time(8, 30), tzinfo=SEOUL))
        assert preopen_service.run_supplement(world.session, clock=clock, sleep=clock.sleep)
        world.session.refresh(pool)
        for stage in ("supplement", "supplement_llm", "theme_refresh"):
            entry = pool.stages[stage]
            assert entry["status"] == "SKIPPED"  # type: ignore[index]
            assert "llm" in entry["detail"]  # type: ignore[index]
        assert clock.at >= datetime.combine(DAY, preopen_service.DEADLINE, tzinfo=SEOUL)

    def test_the_supplement_reads_only_the_pool_since_the_sweep_and_only_new_pairs(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _pool(world)
        swept = datetime.combine(DAY, time(7, 0, 5), tzinfo=SEOUL)
        preopen_service._mark(world.session, pool, "sweep", "SUCCESS")
        preopen_service._note(world.session, pool, "sweep", news_started_at=swept)
        preopen_service._mark(world.session, pool, "llm", "PARTIAL")
        seen: dict[str, Any] = {}
        collectors: list[Any] = []

        def fake_run(collector: Any, _session: Any) -> CollectorRun:
            collectors.append(collector)
            return CollectorRun(
                source=collector.name, started_at=swept, status=CollectorStatus.SUCCESS
            )

        def fake_budget(_session: Any, **kwargs: Any) -> llm_service.BudgetedRun:
            seen["llm"] = kwargs
            return llm_service.BudgetedRun(budget=kwargs["budget"])

        monkeypatch.setattr(preopen_service, "run_collector", fake_run)
        monkeypatch.setattr(preopen_service.llm_service, "run_within_budget", fake_budget)
        monkeypatch.setattr(preopen_service.news_repo, "last_hit_id", lambda _s: 4242)
        monkeypatch.setattr(get_settings(), "llm_schedule_enabled", True)
        clock = Clock(datetime.combine(DAY, time(8, 30), tzinfo=SEOUL))
        preopen_service.run_supplement(world.session, clock=clock, sleep=clock.sleep)

        # 보충 스윕 다음에 테마어 뉴스가 한 번 더 돈다(표시 전용).
        assert [c.name for c in collectors] == ["PREOPEN_NEWS_SUPPLEMENT", "THEME_NEWS"]
        collector = collectors[0]
        assert collector.name == "PREOPEN_NEWS_SUPPLEMENT"
        assert collector.instrument_ids == frozenset(world.ids)
        assert collector.since == swept
        assert seen["llm"]["budget"] == preopen_service.LLM_SUPPLEMENT_BUDGET == 30
        assert seen["llm"]["after_hit_id"] == 4242
        world.session.refresh(pool)
        assert pool.stages["supplement"]["status"] == "SUCCESS"  # type: ignore[index]
        assert pool.stages["supplement_llm"]["status"] == "SUCCESS"  # type: ignore[index]

    def test_scores_wait_for_the_prefetch_and_skip_without_it(self, world: World) -> None:
        pool = _pool(world)
        clock = Clock(datetime.combine(DAY, time(8, 40), tzinfo=SEOUL))
        preopen_service.run_scores(world.session, clock=clock, sleep=clock.sleep)
        world.session.refresh(pool)
        assert pool.stages["score"]["status"] == "SKIPPED"  # type: ignore[index]
        assert "prefetch" in pool.stages["score"]["detail"]  # type: ignore[index]

    def test_the_morning_chain_runs_its_stages_in_order(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        started = datetime.combine(DAY, time(7, 0, 2), tzinfo=SEOUL)

        def fake_run(collector: Any, _session: Any) -> CollectorRun:
            order.append(type(collector).__name__)
            return CollectorRun(
                source=collector.name, started_at=started, status=CollectorStatus.SUCCESS
            )

        class Guard:
            def prune(self) -> int:
                return 0

        # 풀은 여기서 만든 종목으로만 좁힌다.
        real_freeze = preopen_service.freeze

        def narrow(session: Session, pool: PreopenPool, *, asof: datetime, **_: Any) -> Any:
            return real_freeze(session, pool, asof=asof, only=world.ids)

        monkeypatch.setattr(preopen_service, "run_collector", fake_run)
        monkeypatch.setattr(preopen_service, "QuotaGuard", Guard)
        monkeypatch.setattr(preopen_service, "freeze", narrow)
        monkeypatch.setattr(get_settings(), "llm_schedule_enabled", False)
        clock = Clock(MORNING)
        pool = preopen_service.run_morning(world.session, clock=clock)
        assert pool is not None
        world.session.refresh(pool)
        stages = {k: v["status"] for k, v in pool.stages.items()}  # type: ignore[index]
        assert list(stages) == ["sweep", "pool", "search_trends", "prefetch", "llm", "theme_news"]
        assert stages["theme_news"] == "SUCCESS"
        assert stages["sweep"] == stages["pool"] == stages["search_trends"] == "SUCCESS"
        assert stages["llm"] == "SKIPPED"
        assert pool.stages["sweep"]["news_started_at"] == started.isoformat()  # type: ignore[index]
        assert order[:3] == [
            "NaverNewsCollector",
            "DartDisclosureCollector",
            "NaverDataLabCollector",
        ]
        assert pool.asof == MORNING and pool.pool_count == 3
        # 하루 한 번.
        assert preopen_service.run_morning(world.session, clock=clock) is None

    def test_stages_written_from_two_sessions_do_not_undo_each_other(
        self, world: World, engine: object
    ) -> None:
        # 08:40 점수 세션이 풀을 읽은 뒤 07:00 체인이 LLM을 끝내면, 점수 세션이
        # 자기 단계를 적을 때 끝난 LLM을 RUNNING으로 되돌리면 안 된다.
        pool = _pool(world)
        preopen_service._mark(world.session, pool, "llm", "RUNNING")
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
        with factory() as scores:
            theirs = scores.get(PreopenPool, pool.id)
            assert theirs is not None
            preopen_service._mark(scores, theirs, "score", "RUNNING")
            preopen_service._mark(world.session, pool, "llm", "SUCCESS")
            preopen_service._mark(scores, theirs, "score", "SUCCESS")
        world.session.refresh(pool)
        stages = {k: v["status"] for k, v in pool.stages.items()}  # type: ignore[index]
        assert stages["llm"] == "SUCCESS" and stages["score"] == "SUCCESS"

    def test_a_late_chain_stops_once_the_list_is_frozen(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 08:50 목록이 대체 풀로 먼저 얼렸으면, 늦게 끝난 스윕 뒤 체인은 멈춘다.
        started = datetime.combine(DAY, time(7, 0, 2), tzinfo=SEOUL)
        order: list[str] = []

        def fake_run(collector: Any, _session: Any) -> CollectorRun:
            order.append(type(collector).__name__)
            if type(collector).__name__ == "DartDisclosureCollector":
                # 스윕이 끝나기 전에 08:50 목록이 대체 풀로 먼저 얼렸다.
                with sessionmaker(
                    bind=world.session.get_bind(), expire_on_commit=False, future=True
                )() as other:
                    assert preopen_service.take_snapshot(other, now=ASOF, only=world.ids)
            return CollectorRun(
                source=collector.name, started_at=started, status=CollectorStatus.SUCCESS
            )

        class Guard:
            def prune(self) -> int:
                return 0

        monkeypatch.setattr(preopen_service, "run_collector", fake_run)
        monkeypatch.setattr(preopen_service, "QuotaGuard", Guard)
        # `_stop`이 회귀해도 실제 LLM까지 가지 않게 끈다.
        monkeypatch.setattr(get_settings(), "llm_schedule_enabled", False)
        pool = preopen_service.run_morning(world.session, clock=Clock(MORNING))
        assert pool is not None
        world.session.refresh(pool)
        stages = {k: v["status"] for k, v in pool.stages.items()}  # type: ignore[index]
        assert stages["sweep"] == "SUCCESS" and stages["snapshot"] == "SUCCESS"
        # 대체 풀이 끝낸 풀 확정 기록은 늦은 체인이 덮지 않는다.
        assert stages["pool"] == "SUCCESS"
        assert pool.status == DEGRADED_FALLBACK
        for name in ("search_trends", "prefetch", "llm", "theme_news"):
            assert stages[name] == "SKIPPED"
        assert "NaverDataLabCollector" not in order

    def test_a_chain_woken_after_the_open_does_nothing(self, world: World) -> None:
        late = Clock(datetime.combine(DAY, time(9, 5), tzinfo=SEOUL))
        assert preopen_service.run_morning(world.session, clock=late) is None
        assert preopen_service.pool_for(world.session, DAY) is None

    def test_tracked_fundamentals_are_checked_by_the_fundamental_run_only(
        self, engine: object
    ) -> None:
        # 공시 수집(DART_DISCLOSURE)은 `LIKE 'DART%'`에 걸리지만 재무 확인이 아니다.
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
        with factory() as s:
            before = preopen_service.tracked_fundamentals_checked(s)
            future = datetime(2099, 1, 1, tzinfo=UTC)
            s.add(
                CollectorRun(
                    source="DART_DISCLOSURE",
                    started_at=future,
                    finished_at=future,
                    status=CollectorStatus.SUCCESS,
                )
            )
            s.flush()
            assert preopen_service.tracked_fundamentals_checked(s) == before
            s.add(
                CollectorRun(
                    source="DART_FUNDAMENTAL",
                    started_at=future,
                    finished_at=future,
                    status=CollectorStatus.PARTIAL,
                )
            )
            s.flush()
            assert preopen_service.tracked_fundamentals_checked(s) == future
            s.rollback()

    def test_no_chain_on_a_holiday(self, world: World) -> None:
        holiday = Clock(datetime(2025, 6, 3, 7, 0, tzinfo=SEOUL))
        assert preopen_service.run_morning(world.session, clock=holiday) is None
        assert preopen_service.pool_for(world.session, date(2025, 6, 3)) is None


def test_prefetch_counts_nothing_as_fresh_that_is_older_than_the_previous_close() -> None:
    # 기준 날짜 확인용: 6/4의 직전 거래일은 6/2다(6/3 휴장).
    assert preopen_service._previous_session(DAY) == PREVIOUS
    assert KR.session_close(PREVIOUS).astimezone(UTC).date() == PREVIOUS
