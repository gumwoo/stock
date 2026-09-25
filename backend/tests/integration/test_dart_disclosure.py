"""Event disclosures for the whole master from a scripted `list.json`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.base import run_collector
from app.collectors.dart_disclosure import DartDisclosureCollector
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.models import Base, Disclosure, Instrument
from app.models.collector import CollectorStatus

pytestmark = pytest.mark.integration

CORP = "99977701"
SOURCE = "DART_DISCLOSURE_TEST"


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
def listed(engine: object) -> Iterator[tuple[Session, Instrument]]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.KR, name="쀓공시", tracked=False, kr_corp_code=CORP)
        s.add(inst)
        s.commit()
        try:
            yield s, inst
        finally:
            s.rollback()
            # Every scripted receipt, whoever it landed on. They are dated
            # 2000-01-04, and the collector only ever stores the last few
            # days, so no real filing can carry that date.
            s.execute(text("DELETE FROM disclosure WHERE rcept_no LIKE '20000104%'"))
            s.execute(
                text("DELETE FROM disclosure WHERE instrument_id = :i"), {"i": inst.instrument_id}
            )
            s.execute(
                text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": inst.instrument_id}
            )
            s.execute(text("DELETE FROM collector_run WHERE source = :s"), {"s": SOURCE})
            s.commit()


def item(corp: str, rcept: str, title: str) -> dict[str, str]:
    return {"corp_code": corp, "rcept_no": rcept, "report_nm": title, "flr_nm": "테스트"}


def collector(pages: dict[str, list[list[dict[str, str]]]]) -> DartDisclosureCollector:
    c = DartDisclosureCollector(guard=type("G", (), {"reserve": lambda *a, **k: None})())  # type: ignore[arg-type]
    c.name = SOURCE
    c._key = "test-key"
    asked: list[tuple[str, str]] = []

    def fake_get(client: Any, path: str, **params: str) -> dict[str, Any]:
        kind, page = params["pblntf_ty"], int(params["page_no"])
        asked.append((kind, params["page_no"]))
        script = pages.get(kind, [])
        return {
            "status": "000",
            "total_page": len(script) or 1,
            "list": script[page - 1] if page <= len(script) else [],
        }

    c._get = fake_get  # type: ignore[method-assign]
    c.asked = asked  # type: ignore[attr-defined]
    return c


def stored(session: Session, instrument_id: int) -> list[Disclosure]:
    session.expire_all()
    return list(
        session.execute(
            select(Disclosure).where(Disclosure.instrument_id == instrument_id)
        ).scalars()
    )


def test_every_page_of_both_kinds_is_read_and_only_the_master_kept(
    listed: tuple[Session, Instrument],
) -> None:
    session, inst = listed
    c = collector(
        {
            "B": [
                [item(CORP, "20000104999101", "주요사항보고서(자기주식취득결정)")],
                [item("00000000", "20000104999102", "주요사항보고서(유상증자결정)")],
            ],
            "I": [[item(CORP, "20000104999103", "단일판매ㆍ공급계약체결")]],
        }
    )

    run = run_collector(c, session)

    assert run.status is CollectorStatus.SUCCESS
    assert c.asked == [("B", "1"), ("B", "2"), ("I", "1")]  # type: ignore[attr-defined]
    rows = stored(session, inst.instrument_id)
    assert sorted(r.rcept_no for r in rows) == ["20000104999101", "20000104999103"]
    # Not stored against anyone: the filer is not in the master.
    assert (
        session.execute(
            select(Disclosure).where(Disclosure.rcept_no == "20000104999102")
        ).scalar_one_or_none()
        is None
    )


def test_available_from_the_next_open_after_the_receipt_date(
    listed: tuple[Session, Instrument],
) -> None:
    session, inst = listed
    run_collector(collector({"B": [[item(CORP, "20000104999201", "주식소각결정")]]}), session)
    (row,) = stored(session, inst.instrument_id)
    assert row.filed_on == date(2000, 1, 4)
    assert row.available_at == MarketCalendar(Market.KR).next_session_open(date(2000, 1, 4))
    assert row.available_at > datetime(2000, 1, 4, 15, tzinfo=UTC)


def test_a_second_run_stores_nothing_new(listed: tuple[Session, Instrument]) -> None:
    session, inst = listed
    pages = {"B": [[item(CORP, "20000104999301", "주식소각결정")]]}
    run_collector(collector(pages), session)
    again = run_collector(collector(pages), session)
    assert again.items_saved == 0
    assert len(stored(session, inst.instrument_id)) == 1
