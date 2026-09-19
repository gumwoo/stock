"""Point-in-time reconstruction of financial facts.

The claim this project makes about US fundamentals is that it can answer "what
was this figure understood to be on date X", not merely "what is it now". These
tests hold that claim to a real case.

Apple's FY2008 basic EPS was reported as 5.48 in the 2009 10-K and restated to
6.94 in the 2010 10-K, after Apple adopted new revenue-recognition rules
retrospectively. A 27% difference in the same figure for the same year. A
backtest running in mid-2010 that reads 6.94 is not slightly optimistic — it is
using a number that did not exist yet, and every result downstream of it is
fiction.

The fixture data is the genuine SEC payload shape, with the two real filings.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.models import Base, Instrument
from app.models.fundamental import FiscalPeriod, FundamentalSource
from app.repositories import fundamental_repo as repo
from app.repositories.fundamental_repo import FundamentalRow, RevisionPolicy

pytestmark = pytest.mark.integration

US = MarketCalendar(Market.US)

FY2008_END = date(2008, 9, 27)
FY2008_START = date(2007, 9, 30)

# The two genuine filings, as SEC reports them.
FIRST_REPORTED = (date(2009, 10, 27), Decimal("5.48"), "0001193125-09-214859")
RESTATED = (date(2010, 10, 27), Decimal("6.94"), "0001193125-10-238044")


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
def apple(engine: object) -> Iterator[tuple[Session, int]]:
    """An instrument carrying both filings of FY2008 EPS."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.US, name="PIT TEST CORP", us_cik="9999999998")
        s.add(inst)
        s.flush()
        iid = inst.instrument_id

        repo.save_facts(
            s,
            [
                FundamentalRow(
                    instrument_id=iid,
                    taxonomy="us-gaap",
                    concept="EarningsPerShareBasic",
                    unit="USD/shares",
                    period_start=FY2008_START,
                    period_end=FY2008_END,
                    fiscal_year=filed.year,
                    fiscal_period=FiscalPeriod.FY,
                    form="10-K",
                    value=value,
                    filed_at=filed,
                    available_at=US.next_session_open(filed),
                    accession=accn,
                    source=FundamentalSource.SEC,
                )
                for filed, value, accn in (FIRST_REPORTED, RESTATED)
            ],
        )
        s.commit()

        yield s, iid

        s.execute(text("DELETE FROM fundamental WHERE instrument_id = :i"), {"i": iid})
        s.execute(text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": iid})
        s.commit()


def eps(
    session: Session,
    iid: int,
    year: int,
    policy: RevisionPolicy = RevisionPolicy.AS_KNOWN_THEN,
) -> Decimal | None:
    fact = repo.value_as_of(
        session,
        iid,
        "EarningsPerShareBasic",
        asof=datetime(year, 1, 1, tzinfo=UTC),
        period_end=FY2008_END,
        unit="USD/shares",
        policy=policy,
    )
    return fact.value if fact else None


class TestAsKnownThen:
    """What a market participant was actually looking at."""

    def test_nothing_before_the_first_filing(self, apple: tuple[Session, int]) -> None:
        """FY2008 ended in September 2008, but the 10-K was filed in late 2009.

        A backtest in early 2009 had no annual figure for that year, and saying
        so is the correct answer — not falling back to a later filing.
        """
        session, iid = apple
        assert eps(session, iid, 2009) is None

    def test_the_originally_reported_figure_after_the_first_filing(
        self, apple: tuple[Session, int]
    ) -> None:
        session, iid = apple
        assert eps(session, iid, 2010) == Decimal("5.48")

    def test_the_restated_figure_after_the_restatement(self, apple: tuple[Session, int]) -> None:
        session, iid = apple
        assert eps(session, iid, 2011) == Decimal("6.94")

    def test_todays_view_is_the_restated_one(self, apple: tuple[Session, int]) -> None:
        session, iid = apple
        assert eps(session, iid, 2026) == Decimal("6.94")

    def test_the_restatement_is_invisible_before_it_was_filed(
        self, apple: tuple[Session, int]
    ) -> None:
        """The failure this whole table exists to prevent, stated directly."""
        session, iid = apple

        before = eps(session, iid, 2010)
        after = eps(session, iid, 2011)

        assert before == Decimal("5.48")
        assert after == Decimal("6.94")
        assert before != after, "a 27% restatement must not leak backwards in time"


class TestAsFirstReported:
    """What was originally announced, ignoring later restatements."""

    def test_it_never_changes_once_filed(self, apple: tuple[Session, int]) -> None:
        session, iid = apple
        policy = RevisionPolicy.AS_FIRST_REPORTED

        assert eps(session, iid, 2010, policy) == Decimal("5.48")
        assert eps(session, iid, 2011, policy) == Decimal("5.48")
        assert eps(session, iid, 2026, policy) == Decimal("5.48")

    def test_it_still_respects_the_filing_date(self, apple: tuple[Session, int]) -> None:
        """Not-yet-filed is not-yet-filed under either policy."""
        session, iid = apple
        assert eps(session, iid, 2009, RevisionPolicy.AS_FIRST_REPORTED) is None

    def test_the_two_policies_genuinely_differ(self, apple: tuple[Session, int]) -> None:
        """Which is why the caller chooses rather than the repository assuming."""
        session, iid = apple

        known = eps(session, iid, 2011, RevisionPolicy.AS_KNOWN_THEN)
        first = eps(session, iid, 2011, RevisionPolicy.AS_FIRST_REPORTED)

        assert known == Decimal("6.94")
        assert first == Decimal("5.48")


class TestAvailability:
    def test_a_filing_is_not_usable_on_its_filing_date(self, apple: tuple[Session, int]) -> None:
        """SEC gives a filing date with no time of day.

        It cannot distinguish a 06:00 dissemination from a 14:00 one, and under
        Regulation S-T Rule 13 anything transmitted after 17:30 ET is deemed
        filed the next business day anyway. So the boundary is the next session.
        """
        session, iid = apple
        filed_at = FIRST_REPORTED[0]

        on_the_day = repo.value_as_of(
            session,
            iid,
            "EarningsPerShareBasic",
            asof=datetime(filed_at.year, filed_at.month, filed_at.day, 12, 0, tzinfo=UTC),
            period_end=FY2008_END,
            unit="USD/shares",
        )

        assert on_the_day is None, "a filing must not be readable during its own filing date"

    def test_it_becomes_usable_at_the_next_session_open(self, apple: tuple[Session, int]) -> None:
        session, iid = apple
        filed_at = FIRST_REPORTED[0]

        fact = repo.value_as_of(
            session,
            iid,
            "EarningsPerShareBasic",
            asof=US.next_session_open(filed_at),
            period_end=FY2008_END,
            unit="USD/shares",
        )

        assert fact is not None
        assert fact.value == Decimal("5.48")


class TestContextIsolation:
    def test_a_different_unit_is_a_different_series(self, apple: tuple[Session, int]) -> None:
        """Dropping `unit` would let an EPS and a share count collide.

        SEC nests facts as facts[taxonomy][concept]["units"][unit], and the
        same concept name genuinely appears under more than one unit.
        """
        session, iid = apple

        repo.save_facts(
            session,
            [
                FundamentalRow(
                    instrument_id=iid,
                    taxonomy="us-gaap",
                    concept="EarningsPerShareBasic",
                    unit="shares",  # nonsense unit, deliberately
                    period_start=FY2008_START,
                    period_end=FY2008_END,
                    fiscal_year=2009,
                    fiscal_period=FiscalPeriod.FY,
                    form="10-K",
                    value=Decimal("999"),
                    filed_at=FIRST_REPORTED[0],
                    available_at=US.next_session_open(FIRST_REPORTED[0]),
                    accession="different-accn",
                    source=FundamentalSource.SEC,
                )
            ],
        )
        session.commit()

        assert eps(session, iid, 2011) == Decimal("6.94")

    def test_revisions_are_listed_oldest_filing_first(self, apple: tuple[Session, int]) -> None:
        """So a restatement can be shown, not merely survived."""
        session, iid = apple

        history = repo.revisions_of(
            session, iid, "EarningsPerShareBasic", FY2008_END, unit="USD/shares"
        )

        assert [r.value for r in history] == [Decimal("5.48"), Decimal("6.94")]
        assert [r.filed_at for r in history] == [FIRST_REPORTED[0], RESTATED[0]]


class TestIdempotence:
    def test_recollecting_the_same_filing_writes_nothing(self, apple: tuple[Session, int]) -> None:
        """Instantaneous facts have a NULL period_start.

        Postgres's default UNIQUE semantics treat every NULL as distinct, so
        without NULLS NOT DISTINCT those rows duplicate on every run. This was
        a real bug: 1,492 rows across 746 contexts, doubled by one re-run.
        """
        session, iid = apple

        balance = FundamentalRow(
            instrument_id=iid,
            taxonomy="us-gaap",
            concept="Assets",
            unit="USD",
            period_start=None,  # instantaneous
            period_end=FY2008_END,
            fiscal_year=2009,
            fiscal_period=FiscalPeriod.FY,
            form="10-K",
            value=Decimal("39572000000"),
            filed_at=FIRST_REPORTED[0],
            available_at=US.next_session_open(FIRST_REPORTED[0]),
            accession=FIRST_REPORTED[2],
            source=FundamentalSource.SEC,
        )

        assert repo.save_facts(session, [balance]) == 1
        session.commit()
        assert repo.save_facts(session, [balance]) == 0, (
            "a NULL period_start must still count as a duplicate"
        )
        session.commit()
