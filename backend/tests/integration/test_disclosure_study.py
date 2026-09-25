"""공시 이벤트 분석의 표본 만들기: 진입일, 같은 날 공시 합치기, 뺄 날, 지수 대비 값.

2025-06-02(월)에 접수된 공시는 6/3이 휴장(대통령 선거일)이라 6/4가 진입일이다. 종목과 일봉은
여기서 만들고 지운다. 지수 일봉은 로컬 DB에 실제 값이 있을 수 있어, 없을 때만 넣고 넣은 행만
지운다. 기대값은 그래서 DB에 있는 지수 값에서 계산한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.models import Base, Candle, Instrument, Interval, Listing, MarketIndexBar
from app.services import disclosure_study_service as study

pytestmark = pytest.mark.integration

KR = MarketCalendar(Market.KR)
FILED = date(2025, 6, 2)
ENTRY = date(2025, 6, 4)
CORPS = {
    "strong": "99999981",
    "halted": "99999982",
    "wild": "99999983",
    "limit_up": "99999984",
    "intraday": "99999985",
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


def _bar(i: int, day: date, o: float, c: float, volume: float = 1000) -> Candle:
    return Candle(
        instrument_id=i,
        interval=Interval.DAY_1,
        ts=KR.session_open(day),
        available_at=KR.session_close(day),
        open=Decimal(str(o)),
        high=Decimal(str(max(o, c))),
        low=Decimal(str(min(o, c))),
        close=Decimal(str(c)),
        volume=Decimal(str(volume)),
        source="TEST",
    )


class World:
    def __init__(self, session: Session, ids: dict[str, int]) -> None:
        self.session = session
        self.ids = ids


@pytest.fixture
def world(engine: object) -> Iterator[World]:
    assert KR.is_session(FILED) and KR.is_session(ENTRY) and not KR.is_session(date(2025, 6, 3))
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        added = list(
            s.execute(
                pg_insert(MarketIndexBar)
                .values(
                    [
                        {
                            "index_code": "^KS11",
                            "ts": KR.session_open(day),
                            "available_at": KR.session_close(day),
                            "open": Decimal(o),
                            "high": Decimal(max(o, c)),
                            "low": Decimal(min(o, c)),
                            "close": Decimal(c),
                        }
                        for day, o, c in ((FILED, 2690, 2700), (ENTRY, 2727, 2754))
                    ]
                )
                .on_conflict_do_nothing(index_elements=["index_code", "ts"])
                .returning(MarketIndexBar.id)
            ).scalars()
        )
        ids = {}
        for key, corp in CORPS.items():
            inst = Instrument(
                market=Market.KR,
                name=f"쀓공시{key}",
                tracked=False,
                listing=Listing.KOSPI,
                kr_corp_code=corp,
            )
            s.add(inst)
            s.flush()
            ids[key] = inst.instrument_id
        s.add_all(
            [
                _bar(ids["strong"], FILED, 99, 100),
                _bar(ids["strong"], ENTRY, 105, 110),
                _bar(ids["halted"], FILED, 50, 50),
                _bar(ids["halted"], ENTRY, 50, 50, volume=0),
                _bar(ids["wild"], FILED, 10, 10),
                _bar(ids["wild"], ENTRY, 14, 14),
                # 정확히 +30.0% 상한가 갭: 제한폭 안의 실제 거래다.
                _bar(ids["limit_up"], FILED, 1480, 1490),
                _bar(ids["limit_up"], ENTRY, 1937, 1937),
                # 시가 -28%, 종가 +1%: 시가 대비로는 +40%지만 전날 종가 기준 제한폭 안이다.
                _bar(ids["intraday"], FILED, 100, 100),
                _bar(ids["intraday"], ENTRY, 72, 101),
            ]
        )
        s.commit()
        try:
            yield World(s, ids)
        finally:
            s.rollback()
            for i in ids.values():
                s.execute(text("DELETE FROM candle WHERE instrument_id = :i"), {"i": i})
                s.execute(text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": i})
            for i in added:
                s.execute(text("DELETE FROM market_index_bar WHERE id = :i"), {"i": i})
            s.commit()


def _raw() -> dict[str, object]:
    def row(corp: str, n: int, title: str) -> dict[str, str]:
        return {
            "corp_code": corp,
            "rcept_no": f"20250602{n:06d}",
            "report_nm": title,
            "kind": "B",
        }

    return {
        "start": "2025-06-01",
        "end": "2025-06-05",
        "calls": 1,
        "rows": [
            row(CORPS["strong"], 1, "주요사항보고서(자기주식취득결정)"),
            row(CORPS["strong"], 2, "현금ㆍ현물배당결정"),
            row(CORPS["strong"], 2, "현금ㆍ현물배당결정"),  # 같은 접수번호는 한 번만
            row(CORPS["halted"], 3, "주요사항보고서(자기주식취득결정)"),
            row(CORPS["wild"], 4, "주요사항보고서(자기주식취득결정)"),
            row(CORPS["limit_up"], 7, "주요사항보고서(자기주식취득결정)"),
            row(CORPS["intraday"], 8, "주요사항보고서(자기주식취득결정)"),
            row(CORPS["strong"], 5, "임원ㆍ주요주주특정증권등소유상황보고서"),
            row("00000000", 6, "주요사항보고서(자기주식취득결정)"),
        ],
    }


def test_the_next_session_is_measured_and_one_name_day_is_one_event(world: World) -> None:
    found, tally = study.candidates(world.session, _raw(), first_entry=ENTRY, last_entry=ENTRY)
    assert {c.entry for c in found} == {ENTRY}
    assert tally["event"] == 6 and tally["not an event"] == 1 and tally["not in master"] == 1

    sample = study.build_sample(world.session, found, first_entry=ENTRY, last_entry=ENTRY)
    # 가격제한폭은 전날 종가 기준이다. 상한가 갭과 시가 대비 큰 장중 반등은 남고, 제한폭을
    # 넘는 +40% 갭(원가격 데이터의 흔적)만 빠진다.
    assert sorted(e.instrument_id for e in sample.events) == sorted(
        [world.ids["strong"], world.ids["limit_up"], world.ids["intraday"]]
    )
    assert sample.dropped == {
        "merged into the strongest filing": 1,
        "no trading": 1,
        "beyond the price limit": 1,
    }
    event = next(e for e in sample.events if e.instrument_id == world.ids["strong"])
    # 같은 날 두 공시 중 강한 자사주 취득(0.6)이 남는다.
    assert (event.event_type, event.intensity) == ("SHAREHOLDER_RETURN", 0.6)
    assert event.gap == pytest.approx(105 / 100 - 1)
    assert event.open_close == pytest.approx(110 / 105 - 1)
    bars = {
        KR.local_today(ts): (float(o), float(c))
        for ts, o, c in world.session.execute(
            select(MarketIndexBar.ts, MarketIndexBar.open, MarketIndexBar.close).where(
                MarketIndexBar.index_code == "^KS11",
                MarketIndexBar.ts.in_([KR.session_open(FILED), KR.session_open(ENTRY)]),
            )
        ).all()
    }
    assert event.index_gap == pytest.approx(bars[ENTRY][0] / bars[FILED][1] - 1)
    assert event.index_open_close == pytest.approx(bars[ENTRY][1] / bars[ENTRY][0] - 1)


def test_a_filing_whose_entry_falls_outside_the_window_is_not_counted(world: World) -> None:
    found, tally = study.candidates(
        world.session, _raw(), first_entry=date(2025, 6, 5), last_entry=date(2025, 6, 30)
    )
    assert found == [] and tally["entry outside the window"] == 6
