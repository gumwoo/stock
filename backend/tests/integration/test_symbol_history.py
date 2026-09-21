"""Symbol validity windows.

Windows are closed intervals — `valid_from <= asof <= valid_to` — so a
superseded window has to be closed on the day *before* its successor opens.
Closing it on the successor's start date leaves the changeover day resolving to
two instruments at once, which in a multi-year backtest means silently
attributing one company's prices to another.

The code previously set `valid_to = valid_from`, overlapping by exactly one
day, while its own comment said it closed "the day before".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.calendar import Market
from app.models import Base, SymbolHistory
from app.repositories import instrument_repo
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

CIK = fake_cik(__name__)

CHANGEOVER = date(2026, 9, 19)


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
def session(engine: object) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        yield s
        s.execute(text("DELETE FROM symbol_history WHERE symbol IN ('OLDTK','NEWTK')"))
        s.execute(text("DELETE FROM instrument WHERE us_cik = '9999999999'"))
        s.commit()


def rename(session: Session, symbol: str, *, valid_from: date | None = None) -> int:
    inst = instrument_repo.upsert_instrument(
        session,
        market=Market.US,
        name="Boundary Test Corp",
        symbol=symbol,
        us_cik=CIK,
        listed_at=date(2020, 1, 1),
        symbol_valid_from=valid_from,
    )
    session.commit()
    return inst.instrument_id


class TestTickerChange:
    def test_old_window_closes_before_the_new_one_opens(self, session: Session) -> None:
        iid = rename(session, "OLDTK")
        rename(session, "NEWTK", valid_from=CHANGEOVER)

        rows = (
            session.execute(
                select(SymbolHistory)
                .where(SymbolHistory.instrument_id == iid)
                .order_by(SymbolHistory.valid_from)
            )
            .scalars()
            .all()
        )

        old = next(r for r in rows if r.symbol == "OLDTK")
        new = next(r for r in rows if r.symbol == "NEWTK")

        assert old.valid_to == date(2026, 9, 18), "old window must end the day before"
        assert new.valid_from == CHANGEOVER
        assert old.valid_to < new.valid_from

    def test_the_changeover_date_resolves_to_one_symbol(self, session: Session) -> None:
        """The bug in one assertion: on the handover day, only NEWTK matches."""
        rename(session, "OLDTK")
        rename(session, "NEWTK", valid_from=CHANGEOVER)

        matched_old = instrument_repo.resolve_symbol(session, "OLDTK", Market.US, asof=CHANGEOVER)
        matched_new = instrument_repo.resolve_symbol(session, "NEWTK", Market.US, asof=CHANGEOVER)

        assert matched_old is None, "the retired symbol must not match on the changeover date"
        assert matched_new is not None

    def test_the_old_symbol_still_resolves_before_the_change(self, session: Session) -> None:
        """History is preserved: the old mapping was true, and still reads true."""
        iid = rename(session, "OLDTK")
        rename(session, "NEWTK", valid_from=CHANGEOVER)

        earlier = instrument_repo.resolve_symbol(
            session, "OLDTK", Market.US, asof=date(2026, 9, 18)
        )

        assert earlier is not None
        assert earlier.instrument_id == iid

    def test_the_new_symbol_does_not_resolve_before_it_existed(self, session: Session) -> None:
        rename(session, "OLDTK")
        rename(session, "NEWTK", valid_from=CHANGEOVER)

        assert (
            instrument_repo.resolve_symbol(session, "NEWTK", Market.US, asof=date(2026, 9, 18))
            is None
        )

    def test_instrument_identity_survives_the_rename(self, session: Session) -> None:
        """The whole point of instrument_id: same company, different ticker."""
        before = rename(session, "OLDTK")
        after = rename(session, "NEWTK", valid_from=CHANGEOVER)

        assert before == after

    def test_a_rename_is_recorded_as_observed(self, session: Session) -> None:
        """A change we watched happen is more trustworthy than one we inferred."""
        iid = rename(session, "OLDTK")
        rename(session, "NEWTK", valid_from=CHANGEOVER)

        new = (
            session.execute(
                select(SymbolHistory).where(
                    SymbolHistory.instrument_id == iid, SymbolHistory.symbol == "NEWTK"
                )
            )
            .scalars()
            .one()
        )
        assert new.source == "OBSERVED"
