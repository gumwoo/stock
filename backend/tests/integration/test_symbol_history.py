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

import threading
from collections.abc import Iterator
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.calendar import Market
from app.core.clock import utc_now
from app.models import Base, Instrument, SymbolHistory
from app.repositories import instrument_repo
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

CIK = fake_cik(__name__)

CHANGEOVER = date(2026, 9, 19)

# Relative, because a hard-coded future date is a suite with an expiry printed
# on it. `date(2030, 1, 1)` read as "after everything" until 2030, at which
# point three of these assertions quietly invert.
LATER = utc_now().date() + timedelta(days=365)


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


class TestACollectorMayNoticeAChange:
    """A collector reading today's master sees a symbol, not a changeover date.

    The two are different facts and once got the same answer. Every master row
    opens its window on the same placeholder date, so treating "I do not know
    when" as "since the placeholder" makes the replacement start on or before
    the window it replaces — for every company that ever changed code. The
    guard against inverted windows then fires on ordinary operation, and since
    `ValueError` is not a `CollectorError` it escapes `run_collector` and fails
    the whole sweep, permanently, because the placeholder never moves.
    """

    def test_a_second_master_pass_with_a_new_symbol_succeeds(self, session: Session) -> None:
        for symbol in ("990101", "990102"):
            instrument_repo.upsert_instrument(
                session,
                market=Market.KR,
                name="제트제트코드변경",
                symbol=symbol,
                kr_corp_code="ZZ000101",
                symbol_source="MASTER",
            )
            session.commit()

        rows = list(
            session.execute(
                select(SymbolHistory)
                .join(
                    instrument_repo.Instrument,
                    instrument_repo.Instrument.instrument_id == SymbolHistory.instrument_id,
                )
                .where(instrument_repo.Instrument.kr_corp_code == "ZZ000101")
                .order_by(SymbolHistory.valid_from)
            ).scalars()
        )

        assert [r.symbol for r in rows] == ["990101", "990102"]
        assert rows[0].valid_to is not None
        assert rows[0].valid_to < rows[1].valid_from
        assert rows[0].valid_from <= rows[0].valid_to

    def test_both_windows_still_resolve_on_their_own_dates(self, session: Session) -> None:
        for symbol in ("990101", "990102"):
            instrument_repo.upsert_instrument(
                session,
                market=Market.KR,
                name="제트제트코드변경",
                symbol=symbol,
                kr_corp_code="ZZ000101",
                symbol_source="MASTER",
            )
            session.commit()

        old_one = instrument_repo.resolve_symbol(
            session, "990101", Market.KR, asof=date(2020, 1, 1)
        )
        new_one = instrument_repo.resolve_symbol(session, "990102", Market.KR, asof=LATER)

        assert old_one is not None
        assert new_one is not None
        assert old_one.instrument_id == new_one.instrument_id

    def test_reseeding_a_renamed_ticker_does_not_raise(self, session: Session) -> None:
        """`cli seed` passes `listed_at` and no changeover date."""
        for symbol in ("ZZA", "ZZB"):
            instrument_repo.upsert_instrument(
                session,
                market=Market.KR,
                name="제트제트시드변경",
                symbol=symbol,
                kr_corp_code="ZZ000102",
                listed_at=date(2010, 5, 5),
                symbol_source="SEED",
            )
            session.commit()

        found = instrument_repo.resolve_symbol(session, "ZZB", Market.KR, asof=LATER)
        assert found is not None
        assert instrument_repo.current_symbol(session, found.instrument_id) == "ZZB"

        # The closed window must still be a window. `listed_at` is the same
        # date for both passes, so reusing it as the changeover date writes
        # `valid_to` one day before `valid_from` — no exception, and the old
        # ticker then resolves to nothing on any date at all.
        rows = list(
            session.execute(
                select(SymbolHistory)
                .where(SymbolHistory.instrument_id == found.instrument_id)
                .order_by(SymbolHistory.valid_from)
            ).scalars()
        )
        closed = [r for r in rows if r.valid_to is not None]
        assert closed, "the old ticker was not closed at all"
        for row in closed:
            assert row.valid_from <= row.valid_to, (row.symbol, row.valid_from, row.valid_to)
        assert (
            instrument_repo.resolve_symbol(session, "ZZA", Market.KR, asof=date(2010, 6, 1))
            is not None
        )


class TestAnUnanchoredRowIsAdopted:
    def test_a_master_pass_fills_in_the_code_rather_than_duplicating(
        self, session: Session
    ) -> None:
        """Narrowing the name fallback must not create a second row.

        A KR instrument seeded without a corp code, then met by the listing
        master, previously became two rows carrying the same symbol over the
        same dates — after which `resolve_symbol` picks whichever the database
        returns first.
        """
        seeded = instrument_repo.upsert_instrument(
            session, market=Market.KR, name="제트제트무코드", symbol="990103"
        )
        session.commit()
        mastered = instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트무코드",
            symbol="990103",
            kr_corp_code="ZZ000103",
            symbol_source="MASTER",
        )
        session.commit()

        assert seeded.instrument_id == mastered.instrument_id
        assert mastered.kr_corp_code == "ZZ000103"

    def test_an_anchored_namesake_is_still_a_separate_company(self, session: Session) -> None:
        """The control: adoption must not become the merge it replaced."""
        first = instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트동명",
            symbol="990104",
            kr_corp_code="ZZ000104",
        )
        session.commit()
        second = instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트동명",
            symbol="990105",
            kr_corp_code="ZZ000105",
        )
        session.commit()

        assert first.instrument_id != second.instrument_id


class TestTrackedOnAnExistingRow:
    def test_an_existing_row_can_be_marked_tracked(self, session: Session) -> None:
        """The update path had no test; only row creation did."""
        instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트승격",
            symbol="990106",
            kr_corp_code="ZZ000106",
            symbol_source="MASTER",
        )
        session.commit()
        promoted = instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트승격",
            symbol="990106",
            kr_corp_code="ZZ000106",
            tracked=True,
        )
        session.commit()

        assert promoted.tracked is True


class TestAnAnchorIsUnique:
    """One corp code, one company. Enforced by the database, not by hope.

    `upsert_instrument` looks a company up by its anchor and takes the first
    row it finds. Two rows sharing an anchor would make that lookup return
    whichever the planner happened to order first, and the two would then take
    turns owning the same symbol history. The constraint is declared on the
    model; this is here so that removing it fails something.
    """

    def test_the_database_rejects_a_duplicate_corp_code(self, session: Session) -> None:
        session.execute(
            text(
                "INSERT INTO instrument (market, name, tracked, kr_corp_code) "
                "VALUES ('KR', '제트제트앵커A', false, 'ZZ000201')"
            )
        )
        session.commit()

        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO instrument (market, name, tracked, kr_corp_code) "
                    "VALUES ('KR', '제트제트앵커B', false, 'ZZ000201')"
                )
            )
            session.commit()
        session.rollback()

    def test_the_database_rejects_a_duplicate_cik(self, session: Session) -> None:
        session.execute(
            text(
                "INSERT INTO instrument (market, name, tracked, kr_corp_code) "
                "VALUES ('US', '제트제트앵커C', false, 'ZZ000202')"
            )
        )
        session.commit()
        first = session.execute(
            text("SELECT instrument_id FROM instrument WHERE kr_corp_code = 'ZZ000202'")
        ).scalar_one()
        session.execute(
            text("UPDATE instrument SET us_cik = '0009999901' WHERE instrument_id = :i"),
            {"i": first},
        )
        session.commit()

        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO instrument (market, name, tracked, kr_corp_code, us_cik) "
                    "VALUES ('US', '제트제트앵커D', false, 'ZZ000203', '0009999901')"
                )
            )
            session.commit()
        session.rollback()
        session.execute(text("DELETE FROM instrument WHERE us_cik = '0009999901'"))
        session.commit()

    def test_many_rows_may_have_no_anchor_at_all(self, session: Session) -> None:
        """A unique constraint permits repeated NULLs, and has to here.

        Most of what the seed creates for a market we have not mapped yet
        carries no code, and they are different companies.
        """
        for name in ("제트제트무앵커X", "제트제트무앵커Y"):
            session.execute(
                text("INSERT INTO instrument (market, name, tracked) VALUES ('KR', :n, false)"),
                {"n": name},
            )
        session.commit()

        count = session.execute(
            text("SELECT count(*) FROM instrument WHERE name LIKE '제트제트무앵커%'")
        ).scalar_one()
        assert count >= 2

        session.execute(text("DELETE FROM instrument WHERE name LIKE '제트제트무앵커%'"))
        session.commit()


class TestATickerMayBeReassigned:
    """Two companies, one code, different years. Korea recycles six-digit codes.

    Nothing in the feed announces it: the listing master simply shows the code
    against a different company than last month. The danger is not the
    reassignment, it is the window opened for the new holder. A collector that
    does not know when the change happened opens at the placeholder date, which
    is 1970, which covers every day the previous holder legitimately owned the
    code. `resolve_symbol` then matches two rows for a historical date and
    returns whichever the planner ordered first, and a backtest attributes one
    company's prices to another.
    """

    @staticmethod
    def hand_over(session: Session) -> None:
        """Company A gives up 990301; company B picks it up."""
        instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트양도",
            symbol="990301",
            kr_corp_code="ZZ000301",
            symbol_source="MASTER",
        )
        session.commit()
        instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트양도",
            symbol="990302",
            kr_corp_code="ZZ000301",
            symbol_source="MASTER",
        )
        session.commit()
        instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트양수",
            symbol="990301",
            kr_corp_code="ZZ000302",
            symbol_source="MASTER",
        )
        session.commit()

    def test_one_code_resolves_to_one_company_on_any_date(self, session: Session) -> None:
        self.hand_over(session)

        for asof in (date(1999, 1, 1), date(2015, 6, 30), LATER):
            rows = (
                session.execute(
                    select(SymbolHistory).where(
                        SymbolHistory.symbol == "990301",
                        SymbolHistory.valid_from <= asof,
                        (SymbolHistory.valid_to.is_(None)) | (SymbolHistory.valid_to >= asof),
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) <= 1, (
                asof,
                [(r.instrument_id, r.valid_from, r.valid_to) for r in rows],
            )

    def test_the_old_holder_keeps_its_history(self, session: Session) -> None:
        """The previous owner really did have the code. Deleting that is a lie."""
        self.hand_over(session)

        old = instrument_repo.resolve_symbol(session, "990301", Market.KR, asof=date(2015, 6, 30))
        assert old is not None
        assert old.kr_corp_code == "ZZ000301"

    def test_the_new_holder_owns_it_today(self, session: Session) -> None:
        self.hand_over(session)

        now = instrument_repo.resolve_symbol(session, "990301", Market.KR, asof=LATER)
        assert now is not None
        assert now.kr_corp_code == "ZZ000302"

    def test_no_window_ends_before_it_begins(self, session: Session) -> None:
        self.hand_over(session)

        rows = (
            session.execute(
                select(SymbolHistory).where(SymbolHistory.symbol.in_(["990301", "990302"]))
            )
            .scalars()
            .all()
        )

        assert rows
        for row in rows:
            if row.valid_to is not None:
                assert row.valid_from <= row.valid_to, (row.symbol, row.valid_from, row.valid_to)

    def test_the_same_code_in_another_market_is_untouched(self, session: Session) -> None:
        """Six digits mean one company per exchange, not one worldwide."""
        instrument_repo.upsert_instrument(
            session,
            market=Market.US,
            name="제트제트해외",
            symbol="990301",
            us_cik="0009990301",
            symbol_source="MASTER",
        )
        session.commit()
        self.hand_over(session)

        abroad = (
            session.execute(
                select(SymbolHistory)
                .join(Instrument, Instrument.instrument_id == SymbolHistory.instrument_id)
                .where(SymbolHistory.symbol == "990301", Instrument.market == Market.US)
            )
            .scalars()
            .all()
        )

        assert len(abroad) == 1
        assert abroad[0].valid_to is None

        session.execute(
            text(
                "DELETE FROM symbol_history WHERE instrument_id IN "
                "(SELECT instrument_id FROM instrument WHERE us_cik = '0009990301')"
            )
        )
        session.execute(text("DELETE FROM instrument WHERE us_cik = '0009990301'"))
        session.commit()


class TestAFutureListingDate:
    def test_a_change_never_closes_a_window_before_it_opened(self, session: Session) -> None:
        """`listed_at` may be in the future for a company about to list.

        The first window then opens later than today, and a symbol change
        observed today would close it on a day before it began — no exception,
        and the instrument resolves to nothing on every date afterwards.
        """
        ahead = utc_now().date() + timedelta(days=30)
        instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트상장예정",
            symbol="990401",
            kr_corp_code="ZZ000401",
            listed_at=ahead,
        )
        session.commit()
        instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트상장예정",
            symbol="990402",
            kr_corp_code="ZZ000401",
        )
        session.commit()

        rows = (
            session.execute(
                select(SymbolHistory).where(SymbolHistory.symbol.in_(["990401", "990402"]))
            )
            .scalars()
            .all()
        )

        assert len(rows) == 2
        for row in rows:
            if row.valid_to is not None:
                assert row.valid_from <= row.valid_to, (row.symbol, row.valid_from, row.valid_to)


class TestThePreviousHolderSaysNothing:
    """The case the reassignment fix is actually for.

    The earlier test had the old owner move to its new code first, which closes
    its window on the way past — so by the time the newcomer arrives there is
    nothing left to close, and the closing step could be deleted without any
    test noticing. That was measured: removing it left all 954 green.

    What really happens is that the master simply shows the code against
    somebody else. The previous holder is still there, still open, and saying
    nothing.
    """

    @staticmethod
    def take_over(session: Session) -> None:
        instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트원주인",
            symbol="990501",
            kr_corp_code="ZZ000501",
            symbol_source="MASTER",
        )
        session.commit()
        instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트새주인",
            symbol="990501",
            kr_corp_code="ZZ000502",
            symbol_source="MASTER",
        )
        session.commit()

    def test_the_old_window_is_closed(self, session: Session) -> None:
        self.take_over(session)

        rows = list(
            session.execute(
                select(SymbolHistory)
                .where(SymbolHistory.symbol == "990501")
                .order_by(SymbolHistory.valid_from)
            ).scalars()
        )

        assert len(rows) == 2
        assert rows[0].valid_to is not None
        assert rows[1].valid_to is None

    def test_one_company_per_date(self, session: Session) -> None:
        self.take_over(session)

        for asof in (date(1999, 1, 1), date(2020, 6, 1), LATER):
            matched = list(
                session.execute(
                    select(SymbolHistory).where(
                        SymbolHistory.symbol == "990501",
                        SymbolHistory.valid_from <= asof,
                        (SymbolHistory.valid_to.is_(None)) | (SymbolHistory.valid_to >= asof),
                    )
                ).scalars()
            )
            assert len(matched) == 1, (asof, [(r.instrument_id, r.valid_from) for r in matched])

    def test_the_old_holder_still_owns_the_past(self, session: Session) -> None:
        """Closing the window is not enough; it has to close at the right day.

        The newcomer starting the day after the placeholder would satisfy "one
        company per date" and still be a lie: the previous holder really did
        own the code for all those years, and a backtest over them would read
        the newcomer's prices under the old company's name.
        """
        self.take_over(session)

        for asof in (date(1999, 1, 1), date(2015, 6, 30), date(2024, 1, 1)):
            owner = instrument_repo.resolve_symbol(session, "990501", Market.KR, asof=asof)
            assert owner is not None, asof
            assert owner.kr_corp_code == "ZZ000501", (asof, owner.kr_corp_code)

        today = instrument_repo.resolve_symbol(session, "990501", Market.KR, asof=LATER)
        assert today is not None
        assert today.kr_corp_code == "ZZ000502"

    def test_a_duplicate_that_predates_the_lock_is_healed(self, session: Session) -> None:
        """The no-op path used to return before it looked at anyone else.

        That is what made a duplicate permanent. Once two windows existed no
        later pass would notice, because each company found its own symbol
        already current and stopped there. The lock stops new ones from
        forming; rows already in the database from before it have to be
        cleared by the next ordinary pass, so this one is written in raw SQL
        to build the state the repository can no longer produce.
        """
        for code, name in (("ZZ000511", "제트제트중복갑"), ("ZZ000512", "제트제트중복을")):
            iid = session.execute(
                text(
                    "INSERT INTO instrument (market, name, tracked, kr_corp_code) "
                    "VALUES ('KR', :n, false, :c) RETURNING instrument_id"
                ),
                {"n": name, "c": code},
            ).scalar_one()
            session.execute(
                text(
                    "INSERT INTO symbol_history "
                    "(instrument_id, symbol, valid_from, source, ingested_at) "
                    "VALUES (:i, '990511', '1970-01-01', 'MASTER', now())"
                ),
                {"i": iid},
            )
        session.commit()

        before = session.execute(
            text("SELECT count(*) FROM symbol_history WHERE symbol = '990511' AND valid_to IS NULL")
        ).scalar_one()
        assert before == 2, "the fixture did not build the broken state"

        instrument_repo.upsert_instrument(
            session,
            market=Market.KR,
            name="제트제트중복갑",
            symbol="990511",
            kr_corp_code="ZZ000511",
            symbol_source="MASTER",
        )
        session.commit()

        after = session.execute(
            text("SELECT count(*) FROM symbol_history WHERE symbol = '990511' AND valid_to IS NULL")
        ).scalar_one()
        assert after == 1

        session.execute(text("DELETE FROM symbol_history WHERE symbol = '990511'"))
        session.execute(
            text("DELETE FROM instrument WHERE kr_corp_code IN ('ZZ000511','ZZ000512')")
        )
        session.commit()


class TestTwoWritersAtOnce:
    """The race that made a duplicate permanent.

    `_earliest_free` reads "who holds this code", decides, then writes — with
    nothing in the schema to catch two processes doing it at the same moment.
    `symbol_history` has indexes and no uniqueness. Both readers see an empty
    answer, both open a window at the placeholder date, and from then on the
    code resolves to two companies on every date back to 1970. It does not heal
    on its own: a later pass finds its own symbol already current.

    Two `python -m app.cli collect` runs started by hand is all it takes.
    """

    def test_concurrent_upserts_leave_one_open_window(self, engine: object) -> None:
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
        errors: list[BaseException] = []

        def claim(code: str, name: str) -> None:
            try:
                with factory() as s:
                    instrument_repo.upsert_instrument(
                        session=s,
                        market=Market.KR,
                        name=name,
                        symbol="990601",
                        kr_corp_code=code,
                        symbol_source="MASTER",
                    )
                    s.commit()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=claim, args=("ZZ000601", "제트제트경합갑")),
            threading.Thread(target=claim, args=("ZZ000602", "제트제트경합을")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], errors

        with factory() as s:
            open_rows = list(
                s.execute(
                    select(SymbolHistory).where(
                        SymbolHistory.symbol == "990601", SymbolHistory.valid_to.is_(None)
                    )
                ).scalars()
            )
            assert len(open_rows) == 1, [(r.instrument_id, r.valid_from) for r in open_rows]

            for asof in (date(1999, 1, 1), LATER):
                matched = list(
                    s.execute(
                        select(SymbolHistory).where(
                            SymbolHistory.symbol == "990601",
                            SymbolHistory.valid_from <= asof,
                            (SymbolHistory.valid_to.is_(None)) | (SymbolHistory.valid_to >= asof),
                        )
                    ).scalars()
                )
                assert len(matched) <= 1, (asof, [r.instrument_id for r in matched])

            s.execute(
                text(
                    "DELETE FROM symbol_history WHERE instrument_id IN "
                    "(SELECT instrument_id FROM instrument WHERE kr_corp_code IN "
                    "('ZZ000601','ZZ000602'))"
                )
            )
            s.execute(text("DELETE FROM instrument WHERE kr_corp_code IN ('ZZ000601','ZZ000602')"))
            s.commit()

    def test_a_whole_master_load_takes_one_lock(self, session: Session) -> None:
        """Per-ticker keys would pile four thousand locks into one transaction.

        The shared table holds `max_locks_per_transaction` times
        `max_connections` entries, 6,400 by default. Two master loads at once
        would exhaust it and fail — the exact situation the lock is for.
        """
        for n in range(40):
            instrument_repo.upsert_instrument(
                session,
                market=Market.KR,
                name=f"제트제트다량{n:03d}",
                symbol=f"9907{n:02d}",
                kr_corp_code=f"ZZ0007{n:02d}",
                symbol_source="MASTER",
            )
        held = session.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid()"
            )
        ).scalar_one()
        session.commit()

        assert held == 1, held

        session.execute(text("DELETE FROM symbol_history WHERE symbol LIKE '9907%'"))
        session.execute(text("DELETE FROM instrument WHERE kr_corp_code LIKE 'ZZ0007%'"))
        session.commit()
