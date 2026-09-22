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
        # Bound to the same CIK the fixture creates, not to a literal. The
        # literal was 9999999999 and stayed behind when the module moved to a
        # derived identifier, so every run left another Boundary Test Corp in
        # the development database and nothing failed.
        s.execute(text("DELETE FROM instrument WHERE us_cik = :cik"), {"cik": CIK})
        s.execute(
            text(
                "DELETE FROM symbol_history WHERE instrument_id IN "
                "(SELECT instrument_id FROM instrument WHERE kr_corp_code LIKE 'ZZ%')"
            )
        )
        s.execute(text("DELETE FROM instrument WHERE kr_corp_code LIKE 'ZZ%'"))
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


class TestTwoCompaniesOneName:
    """Korean company names are not unique.

    The real `corpCode.xml` holds thirty pairs of listed companies sharing a
    name, SK and 삼성물산 among them. Matching on the name after a corp code
    was already given would put both on one row, overwrite the corp code with
    the second one's, and then close the symbol window on a day before it
    opened — after which neither code resolves to anything, ever.
    """

    def test_a_shared_name_with_different_corp_codes_stays_two_rows(self, session: Session) -> None:
        first = instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트테스트",
            symbol="990001",
            kr_corp_code="ZZ000001",
            symbol_source="MASTER",
        )
        second = instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트테스트",
            symbol="990002",
            kr_corp_code="ZZ000002",
            symbol_source="MASTER",
        )
        session.commit()

        assert first.instrument_id != second.instrument_id
        assert first.kr_corp_code == "ZZ000001"
        assert second.kr_corp_code == "ZZ000002"

    def test_each_one_still_resolves_to_its_own_symbol(self, session: Session) -> None:
        for code, symbol in (("ZZ000001", "990001"), ("ZZ000002", "990002")):
            instrument_repo.upsert_instrument(
                session,
                market=Market.KR,
                name="제트제트테스트",
                symbol=symbol,
                kr_corp_code=code,
                symbol_source="MASTER",
            )
        session.commit()

        for symbol in ("990001", "990002"):
            found = instrument_repo.resolve_symbol(
                session, symbol, Market.KR, asof=date(2026, 9, 22)
            )
            assert found is not None, f"{symbol} resolves to nothing"

    def test_a_name_without_an_anchor_still_finds_its_row(self, session: Session) -> None:
        """The fallback has a job; narrowing it must not remove the job."""
        made = instrument_repo.upsert_instrument(
            session, market=Market.KR, name="제트제트무앵커", symbol="990003"
        )
        session.commit()
        again = instrument_repo.upsert_instrument(
            session, market=Market.KR, name="제트제트무앵커", symbol="990003"
        )
        session.commit()

        assert made.instrument_id == again.instrument_id
        session.execute(
            text("DELETE FROM symbol_history WHERE instrument_id = :i"),
            {"i": made.instrument_id},
        )
        session.execute(
            text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": made.instrument_id}
        )
        session.commit()


class TestAWindowMayNotEndBeforeItBegins:
    def test_a_backdated_symbol_is_refused(self, session: Session) -> None:
        """Written, it closes the live window on a day earlier than its start.

        `resolve_symbol` then matches neither row and the instrument has no
        symbol on any date at all. A refusal is loud; the write is silent and
        permanent.
        """
        instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트역전",
            symbol="990004",
            kr_corp_code="ZZ000004",
            symbol_valid_from=date(2020, 1, 1),
        )
        session.commit()

        with pytest.raises(ValueError, match="on or before"):
            instrument_repo.upsert_instrument(
                session,
                market=Market.KR,
                name="제트제트역전",
                symbol="990005",
                kr_corp_code="ZZ000004",
                symbol_valid_from=date(2019, 1, 1),
            )
        session.rollback()


class TestTrackedIsSetWhereItIsMeant:
    def test_a_master_row_starts_untracked(self, session: Session) -> None:
        """A listing master says a company exists, not that anyone follows it."""
        made = instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트마스터",
            symbol="990006",
            kr_corp_code="ZZ000006",
            symbol_source="MASTER",
        )
        session.commit()

        assert made.tracked is False

    def test_seeding_marks_the_row_tracked(self, session: Session) -> None:
        """Otherwise the column default wins and `score_all` scores nothing."""
        made = instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트시드",
            symbol="990007",
            kr_corp_code="ZZ000007",
            tracked=True,
        )
        session.commit()

        assert made.tracked is True

    def test_a_later_master_pass_does_not_untrack_it(self, session: Session) -> None:
        instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트유지",
            symbol="990008",
            kr_corp_code="ZZ000008",
            tracked=True,
        )
        session.commit()
        again = instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트유지",
            symbol="990008",
            kr_corp_code="ZZ000008",
            symbol_source="MASTER",
        )
        session.commit()

        assert again.tracked is True
