"""Minute bars stored from scripted KIS pages: whole days, settled days left alone, bounded calls.

Stock bars hang off an instrument this module creates. Index bars are stored
for 2000-01-04, a day no real collection can reach (the provider serves only
its latest session), and removed by that date afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.collectors import kis_minute
from app.collectors.base import run_collector
from app.collectors.kis_minute import (
    INDEX_PATH,
    STOCK_PATH,
    KisIndexMinuteCollector,
    KisMinuteCollector,
    minute_start,
)
from app.collectors.quota import QuotaExhausted
from app.config import get_settings
from app.core.calendar import Market
from app.core.quota import LimitSource, Quota
from app.models import Base, Instrument, SymbolHistory
from app.models.collector import CollectorStatus
from app.models.intraday import IndexMinuteBar, MinuteBar, MinuteFetch

pytestmark = pytest.mark.integration

DAY = date(2026, 9, 23)
OLD_DAY = date(2000, 1, 4)
SOURCES = ("KIS_MINUTE_TEST", "KIS_INDEX_MINUTE_TEST")


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
def world(engine: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    monkeypatch.setattr(get_settings(), "kis_app_key", "k")
    monkeypatch.setattr(get_settings(), "kis_app_secret", "s")
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        ids = []
        for n, name in enumerate(("쀓분봉가", "쀓분봉나")):
            inst = Instrument(market=Market.KR, name=name, tracked=False)
            s.add(inst)
            s.flush()
            s.add(
                SymbolHistory(
                    instrument_id=inst.instrument_id,
                    symbol=f"99097{n}",
                    valid_from=date(2000, 1, 1),
                    source="SEED",
                )
            )
            ids.append(inst.instrument_id)
        s.commit()
        try:
            yield World(s, ids)
        finally:
            s.rollback()
            for i in ids:
                s.execute(text("DELETE FROM symbol_history WHERE instrument_id = :i"), {"i": i})
                s.execute(text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": i})
            s.execute(text("DELETE FROM index_minute_bar WHERE session_date = :d"), {"d": OLD_DAY})
            for source in SOURCES:
                s.execute(text("DELETE FROM collector_run WHERE source = :s"), {"s": source})
            s.commit()


def day_labels() -> list[str]:
    return ["153000"] + [
        f"{m // 60:02d}{m % 60:02d}00" for m in (15 * 60 + 19 - i for i in range(380))
    ]


def stock_row(day: str, label: str) -> dict[str, str]:
    return {
        "stck_bsop_date": day,
        "stck_cntg_hour": label,
        "stck_prpr": "1000",
        "stck_oprc": "1000",
        "stck_hgpr": "1001",
        "stck_lwpr": "999",
        "cntg_vol": "5",
        "acml_tr_pbmn": "0",
    }


class FakeKis:
    """Answers stock and index minute calls from a script; counts every call."""

    def __init__(self, *, refuse_after: int | None = None, stuck: set[str] | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self.refuse_after = refuse_after
        self.stuck = stuck or set()
        self.index_page: list[dict[str, str]] = []

    def get(self, path: str, *, tr_id: str, params: dict[str, str], tr_cont: str = "") -> Any:
        if self.refuse_after is not None and len(self.calls) >= self.refuse_after:
            quota = Quota(
                key="t",
                group="kis_rest",
                official_limit=2,
                window=timedelta(days=1),
                limit_source=LimitSource.INTERNAL,
                note="test",
            )
            raise QuotaExhausted(quota=quota, spent=1, allowed=1, retry_after=None)
        self.calls.append(dict(params))
        if path == INDEX_PATH:
            return {"rt_cd": "0", "output2": self.index_page}, ""
        assert path == STOCK_PATH
        day, cursor = params["FID_INPUT_DATE_1"], params["FID_INPUT_HOUR_1"]
        if params["FID_INPUT_ISCD"] in self.stuck:
            return {"rt_cd": "0", "output2": [stock_row(day, "153000")]}, ""
        stream = [stock_row(day, lab) for lab in day_labels()]
        stream += [stock_row("19990101", "155900")] * 10
        start = next(
            i
            for i, r in enumerate(stream)
            if r["stck_cntg_hour"] <= cursor or r["stck_bsop_date"] != day
        )
        return {"rt_cd": "0", "output2": stream[start : start + 120]}, ""

    def close(self) -> None:  # pragma: no cover - the collector closes only its own
        pass


def collector(
    world: World, fake: FakeKis, *, days: list[date], max_calls: int = 1000
) -> KisMinuteCollector:
    c = KisMinuteCollector(instrument_ids=world.ids, max_calls=max_calls, client=fake)  # type: ignore[arg-type]
    c.name = SOURCES[0]
    c.days = lambda now: days  # type: ignore[method-assign]
    return c


def fetches(world: World) -> list[tuple[int, date, str, int]]:
    world.session.expire_all()
    return [
        (f.instrument_id, f.session_date, f.status, f.bars)
        for f in world.session.execute(
            select(MinuteFetch)
            .where(MinuteFetch.instrument_id.in_(world.ids))
            .order_by(MinuteFetch.id)
        ).scalars()
    ]


def bars(world: World, instrument_id: int) -> list[MinuteBar]:
    world.session.expire_all()
    return list(
        world.session.execute(
            select(MinuteBar).where(MinuteBar.instrument_id == instrument_id).order_by(MinuteBar.ts)
        ).scalars()
    )


class TestStockDays:
    def test_a_whole_day_is_stored_minute_by_minute(self, world: World) -> None:
        fake = FakeKis()
        run = run_collector(collector(world, fake, days=[DAY]), world.session)
        assert run.status is CollectorStatus.SUCCESS
        stored = bars(world, world.ids[0])
        assert len(stored) == 381
        assert stored[0].ts == minute_start(DAY, "090000")
        assert stored[-1].ts == minute_start(DAY, "153000")
        assert all(b.available_at == b.ts + timedelta(minutes=1) for b in stored)
        assert {(i, s, n) for i, _, s, n in fetches(world)} == {
            (i, "COMPLETE", 381) for i in world.ids
        }
        assert len(fake.calls) == 8  # four pages each

    def test_a_settled_day_is_not_asked_for_again(self, world: World) -> None:
        run_collector(collector(world, FakeKis(), days=[DAY]), world.session)
        again = FakeKis()
        run_collector(collector(world, again, days=[DAY]), world.session)
        assert again.calls == []

    def test_a_partial_day_is_recorded_and_asked_for_again(self, world: World) -> None:
        stuck = FakeKis(stuck={"990970"})
        run = run_collector(collector(world, stuck, days=[DAY]), world.session)
        assert run.status is CollectorStatus.PARTIAL
        statuses = {i: s for i, _, s, _ in fetches(world)}
        assert statuses[world.ids[0]] == "PARTIAL" and statuses[world.ids[1]] == "COMPLETE"
        retry = FakeKis()
        run_collector(collector(world, retry, days=[DAY]), world.session)
        assert {c["FID_INPUT_ISCD"] for c in retry.calls} == {"990970"}

    def test_the_call_cap_stops_before_a_day_it_might_not_finish(self, world: World) -> None:
        fake = FakeKis()
        run = run_collector(collector(world, fake, days=[DAY], max_calls=10), world.session)
        # One day needs up to eight pages; the second would pass the cap.
        assert run.status is CollectorStatus.PARTIAL
        assert len(fake.calls) == 4
        assert len(fetches(world)) == 1

    def test_the_quota_stopping_keeps_the_days_already_stored(self, world: World) -> None:
        fake = FakeKis(refuse_after=5)
        run = run_collector(collector(world, fake, days=[DAY]), world.session)
        assert run.status is CollectorStatus.PARTIAL
        assert [s for _, _, s, _ in fetches(world)] == ["COMPLETE"]
        assert len(bars(world, world.ids[0])) == 381


class TestIndexPieces:
    def test_todays_minutes_are_stored_under_the_daily_codes(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(kis_minute, "utc_now", lambda: datetime(2000, 1, 4, 6, 0, tzinfo=UTC))
        fake = FakeKis()
        key = OLD_DAY.strftime("%Y%m%d")
        fake.index_page = [
            {
                "stck_bsop_date": key,
                "stck_cntg_hour": h,
                "bstp_nmix_prpr": "700",
                "bstp_nmix_oprc": "700",
                "bstp_nmix_hgpr": "700",
                "bstp_nmix_lwpr": "700",
                "cntg_vol": "1",
                "acml_tr_pbmn": "1",
            }
            for h in ("999999", "888888", "150000", "145900")
        ]
        c = KisIndexMinuteCollector(client=fake)  # type: ignore[arg-type]
        c.name = SOURCES[1]
        run = run_collector(c, world.session)
        assert run.status is CollectorStatus.SUCCESS
        world.session.expire_all()
        got = world.session.execute(
            select(IndexMinuteBar.index_code, IndexMinuteBar.ts).where(
                IndexMinuteBar.session_date == OLD_DAY
            )
        ).all()
        assert {code for code, _ in got} == {"^KS11", "^KQ11"}
        assert len(got) == 4
        # Asked again, nothing is stored twice.
        assert run_collector(c, world.session).items_saved == 0


class TestRobustness:
    def test_one_names_bad_answer_is_recorded_and_the_rest_goes_on(self, world: World) -> None:
        class Broken(FakeKis):
            def get(
                self, path: str, *, tr_id: str, params: dict[str, str], tr_cont: str = ""
            ) -> Any:
                body, cont = super().get(path, tr_id=tr_id, params=params, tr_cont=tr_cont)
                if params["FID_INPUT_ISCD"] == "990970":
                    body["output2"] = [dict(body["output2"][0], stck_prpr="garbage")]
                return body, cont

        run = run_collector(collector(world, Broken(), days=[DAY]), world.session)
        assert run.status is CollectorStatus.PARTIAL
        statuses = {i: s for i, _, s, _ in fetches(world)}
        assert statuses == {world.ids[0]: "ERROR", world.ids[1]: "COMPLETE"}
        retry = FakeKis()
        run_collector(collector(world, retry, days=[DAY]), world.session)
        assert {c["FID_INPUT_ISCD"] for c in retry.calls} == {"990970"}

    def test_an_empty_today_is_not_settled(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(kis_minute, "utc_now", lambda: datetime(2026, 9, 23, 8, 0, tzinfo=UTC))

        class NothingYet(FakeKis):
            def get(
                self, path: str, *, tr_id: str, params: dict[str, str], tr_cont: str = ""
            ) -> Any:
                self.calls.append(dict(params))
                return {"rt_cd": "0", "output2": [stock_row("20260922", "155900")]}, ""

        run_collector(collector(world, NothingYet(), days=[DAY]), world.session)
        assert {s for _, _, s, _ in fetches(world)} == {"PARTIAL"}

    def test_a_second_kis_run_at_the_same_time_is_skipped(self, world: World) -> None:
        from app.db import advisory_lock, session_scope

        fake = FakeKis()
        with session_scope() as other, advisory_lock(other, kis_minute.KIS_RUN_LOCK) as held:
            assert held
            run = run_collector(collector(world, fake, days=[DAY]), world.session)
        assert run.status is CollectorStatus.SKIPPED
        assert fake.calls == []
