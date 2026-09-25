"""Search trends stored per name, read as of a moment, and filed beside a signal.

The provider is scripted: `_post` is replaced and the guard counts. Every row
hangs off instruments this module creates and is removed with them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.base import run_collector
from app.collectors.naver_datalab import NaverDataLabCollector
from app.collectors.quota import QuotaExhausted
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.quota import LimitSource, Quota
from app.core.types import MissingFactorPolicy, SignalAction
from app.models import Base, Instrument, SearchTrend, Signal
from app.models.collector import CollectorStatus
from app.repositories import attention_repo
from app.scoring.attention import Status
from app.services import attention_service

pytestmark = pytest.mark.integration

KR = MarketCalendar(Market.KR)
SOURCE = "NAVER_DATALAB_TEST"


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
    def __init__(self, session: Session, ids: list[int]) -> None:
        self.session = session
        self.ids = ids


@pytest.fixture
def world(engine: object) -> Iterator[World]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        made = [
            Instrument(market=Market.KR, name="쀓관심가", tracked=False),
            Instrument(market=Market.KR, name="쀓관심나", tracked=False),
            Instrument(market=Market.US, name="쀓관심US", tracked=False),
        ]
        s.add_all(made)
        s.commit()
        ids = [i.instrument_id for i in made]
        try:
            yield World(s, ids)
        finally:
            s.rollback()
            for i in ids:
                s.execute(text("DELETE FROM signal WHERE instrument_id = :i"), {"i": i})
                s.execute(text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": i})
            s.execute(text("DELETE FROM collector_run WHERE source = :s"), {"s": SOURCE})
            s.commit()


def db_now(session: Session) -> datetime:
    return session.execute(text("SELECT clock_timestamp()")).scalar_one()


class Guard:
    def __init__(self, allow: int = 100) -> None:
        self.allow = allow
        self.reserved = 0

    def reserve(self, group: str, endpoint: str, **_: Any) -> None:
        if self.reserved >= self.allow:
            quota = Quota(
                key="t",
                group=group,
                official_limit=2,
                window=timedelta(days=1),
                limit_source=LimitSource.INTERNAL,
                note="test",
            )
            raise QuotaExhausted(quota=quota, spent=1, allowed=1, retry_after=None)
        assert (group, endpoint) == ("naver_datalab", "search_trend")
        self.reserved += 1


def collector(world: World, ids: list[int], answers: dict[str, list[Any]], guard: Guard) -> Any:
    c = NaverDataLabCollector(instrument_ids=ids, guard=guard)  # type: ignore[arg-type]
    c.name = SOURCE
    c._client_id = c._client_secret = "test"
    asked: list[dict[str, Any]] = []

    def post(client: Any, body: dict[str, Any]) -> dict[str, Any]:
        c._guard.reserve("naver_datalab", "search_trend")
        asked.append(body)
        group = body["keywordGroups"][0]["groupName"]
        return {"results": [{"title": group, "data": answers.get(group, [])}]}

    c._post = post
    c.asked = asked
    return c


def trends(world: World, instrument_id: int) -> list[SearchTrend]:
    world.session.expire_all()
    return list(
        world.session.execute(
            select(SearchTrend).where(SearchTrend.instrument_id == instrument_id)
        ).scalars()
    )


class TestCollection:
    def test_one_request_per_name_in_focus_and_nothing_else(self, world: World) -> None:
        a, b, _ = world.ids
        guard = Guard()
        c = collector(world, [a], {"쀓관심가": [{"period": "2026-09-22", "ratio": 100}]}, guard)

        run = run_collector(c, world.session)

        assert run.status is CollectorStatus.SUCCESS
        assert guard.reserved == 1 and len(c.asked) == 1
        assert c.asked[0]["keywordGroups"][0]["keywords"] == [
            "쀓관심가 주가",
            "쀓관심가주가",
            "쀓관심가 주식",
        ]
        assert c.asked[0]["timeUnit"] == "date"
        (row,) = trends(world, a)
        assert row.series == {"2026-09-22": 100.0}
        assert trends(world, b) == []

    def test_a_name_too_little_searched_is_stored_empty(self, world: World) -> None:
        a = world.ids[0]
        run_collector(collector(world, [a], {}, Guard()), world.session)
        (row,) = trends(world, a)
        assert row.series == {}

    def test_the_quota_stops_the_run_partway_as_partial(self, world: World) -> None:
        a, b, _ = world.ids
        run = run_collector(collector(world, [a, b], {}, Guard(allow=1)), world.session)
        assert run.status is CollectorStatus.PARTIAL
        assert len(trends(world, a)) + len(trends(world, b)) == 1

    def test_one_names_bad_answer_does_not_stop_the_others(self, world: World) -> None:
        a, b, _ = world.ids
        c = collector(
            world,
            [a, b],
            {"쀓관심가": [{"period": "2026-09-22", "ratio": "bad"}], "쀓관심나": []},
            Guard(),
        )
        run = run_collector(c, world.session)
        assert run.status is CollectorStatus.PARTIAL
        assert trends(world, a) == [] and len(trends(world, b)) == 1

    def test_the_quota_refusing_the_first_call_skips_the_run(self, world: World) -> None:
        a = world.ids[0]
        run = run_collector(collector(world, [a], {}, Guard(allow=0)), world.session)
        assert run.status is CollectorStatus.SKIPPED
        assert trends(world, a) == []


def store(
    world: World,
    instrument_id: int,
    series: dict[str, float],
    *,
    days: int = 60,
    end: date | None = None,
) -> SearchTrend:
    today = KR.local_today(db_now(world.session))
    row = attention_repo.save_trend(
        world.session,
        instrument_id=instrument_id,
        start_date=today - timedelta(days=days),
        end_date=end or today,
        keywords=["x"],
        series=series,
    )
    world.session.commit()
    return row


def flat_then(world: World, recent: float) -> dict[str, float]:
    """Level 10 on every session, `recent` on the last three sessions before today."""
    today = KR.local_today(db_now(world.session))
    sessions = KR.sessions_between(today - timedelta(days=60), today - timedelta(days=1))
    series = {d.isoformat(): 10.0 for d in sessions}
    for d in sessions[-3:]:
        series[d.isoformat()] = recent
    return series


class TestAtAMoment:
    def test_the_surge_of_the_newest_fetch(self, world: World) -> None:
        a = world.ids[0]
        trend = store(world, a, flat_then(world, 32.0))
        found, trend_id = attention_service.attention_at(world.session, a, db_now(world.session))
        assert trend_id == trend.id
        assert found.status == Status.MEASURED
        assert found.surge == pytest.approx(33 / 11)

    def test_a_fetch_that_stops_before_the_last_session_is_stale(self, world: World) -> None:
        a = world.ids[0]
        today = KR.local_today(db_now(world.session))
        last = KR.sessions_between(today - timedelta(days=14), today - timedelta(days=1))[-1]
        store(world, a, flat_then(world, 32.0), end=last - timedelta(days=1))
        found, trend_id = attention_service.attention_at(world.session, a, db_now(world.session))
        assert found.status == Status.STALE and found.surge is None and trend_id is not None

    def test_a_fetch_stored_after_the_moment_is_invisible(self, world: World) -> None:
        a = world.ids[0]
        before = db_now(world.session)
        store(world, a, flat_then(world, 32.0))
        found, trend_id = attention_service.attention_at(world.session, a, before)
        assert (found.status, trend_id) == (Status.NO_FETCH, None)

    def test_the_day_of_the_moment_is_not_read_even_when_the_fetch_has_it(
        self, world: World
    ) -> None:
        # A trading day, so that it would be among the sessions read: at its
        # close its searches are not over.
        a = world.ids[0]
        day = KR.next_session(KR.local_today(db_now(world.session)))
        series = flat_then(world, 10.0)
        between = KR.sessions_between(KR.local_today(db_now(world.session)), day)
        for d in between:
            series[d.isoformat()] = 10.0
        series[day.isoformat()] = 1000.0
        store(world, a, series, end=day)
        found, _ = attention_service.attention_at(world.session, a, KR.session_close(day))
        assert found.surge == pytest.approx(1.0)


def signal_for(world: World, instrument_id: int) -> Signal:
    now = db_now(world.session)
    row = Signal(
        instrument_id=instrument_id,
        data_asof=now,
        decision_at=now,
        earliest_execution_at=now + timedelta(hours=18),
        total_score=55.0,
        action=SignalAction.WATCH,
        policy=MissingFactorPolicy.ZERO,
        reasons=[],
        strategy_version="attention-test",
    )
    world.session.add(row)
    world.session.commit()
    return row


class TestBesideTheSignal:
    def test_filed_for_a_korean_name_and_the_signal_is_untouched(self, world: World) -> None:
        a = world.ids[0]
        store(world, a, flat_then(world, 32.0))
        row = signal_for(world, a)
        filed = attention_service.attach(world.session, row)
        assert filed is not None
        assert (filed.status, filed.asof) == (Status.MEASURED, row.decision_at)
        assert filed.surge == pytest.approx(33 / 11)
        world.session.expire_all()
        stored = world.session.get(Signal, row.id)
        assert stored is not None and (stored.total_score, stored.action) == (
            55.0,
            SignalAction.WATCH,
        )

    def test_without_a_fetch_the_record_says_so(self, world: World) -> None:
        filed = attention_service.attach(world.session, signal_for(world, world.ids[1]))
        assert filed is not None and (filed.status, filed.trend_id) == (Status.NO_FETCH, None)

    def test_a_name_outside_korea_has_no_row(self, world: World) -> None:
        assert attention_service.attach(world.session, signal_for(world, world.ids[2])) is None

    def test_a_failure_costs_the_signal_nothing(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = signal_for(world, world.ids[0])

        def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("attention broke")

        monkeypatch.setattr(attention_service, "attention_at", broken)
        assert attention_service.attach(world.session, row) is None
        world.session.expire_all()
        assert world.session.get(Signal, row.id) is not None
