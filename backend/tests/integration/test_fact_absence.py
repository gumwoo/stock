"""What an absent value is allowed to claim.

An earlier design had two states and therefore over-claimed. `coverage_start`
was computed across the whole instrument, so once *any* fact existed the code
treated a missing one as proof of non-publication. For Apple that boundary is
2009-07-22, which made a 2009-08-01 lookup for FY2008 EPS answer "not filed
yet" — while the report carrying that figure had been filed on 2008-11-05 and
the market had known it for nine months.

Absence now has to earn its claim, weakest first:

    SOURCE_COVERAGE_UNAVAILABLE   the value source does not reach this era
    NO_OBSERVATION_IN_SOURCE      it does, but holds nothing for this context
    NOT_YET_FILED                 the filing register shows no covering report

Only the last says anything about the world, and it is only reachable because
the filing register is collected from SEC submissions, which lists reports back
to the 1990s including ones XBRL never tagged.
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
from app.repositories import filing_repo
from app.repositories import fundamental_repo as repo
from app.repositories.filing_repo import FilingRow
from app.repositories.fundamental_repo import (
    FactOutcome,
    FundamentalContext,
    FundamentalRow,
)

pytestmark = pytest.mark.integration

US = MarketCalendar(Market.US)

FY2008 = FundamentalContext(
    taxonomy="us-gaap",
    concept="EarningsPerShareBasic",
    unit="USD/shares",
    period_end=date(2008, 9, 27),
    period_start=date(2007, 9, 30),
)

FY2009 = FundamentalContext(
    taxonomy="us-gaap",
    concept="EarningsPerShareBasic",
    unit="USD/shares",
    period_end=date(2009, 9, 26),
    period_start=date(2008, 9, 28),
)


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
def company(engine: object) -> Iterator[tuple[Session, int]]:
    """The real shape of the problem: a register older than the value source."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.US, name="ABSENCE TEST CORP", us_cik="9999999997")
        s.add(inst)
        s.flush()
        iid = inst.instrument_id

        # The register knows about the pre-XBRL annual reports.
        filing_repo.save_filings(
            s,
            [
                FilingRow(
                    instrument_id=iid,
                    form="10-K",
                    filed_at=filed,
                    period_of_report=period,
                    available_at=US.next_session_open(filed),
                    accession=accn,
                    source=FundamentalSource.SEC,
                )
                for filed, period, accn in (
                    (date(2007, 11, 15), date(2007, 9, 29), "0001047469-07-009340"),
                    (date(2008, 11, 5), date(2008, 9, 27), "0001193125-08-224958"),
                    (date(2009, 10, 27), date(2009, 9, 26), "0001193125-09-214859"),
                )
            ],
        )

        # The value source starts tagging mid-2009 — Apple's earliest tagged
        # fact of any kind is dated 2009-07-22, from a quarterly report. This
        # row exists so the fixture reproduces the real shape: value coverage
        # begins *before* the FY2008 question is asked, which is precisely what
        # made the earlier two-state design over-claim.
        repo.save_facts(
            s,
            [
                FundamentalRow(
                    instrument_id=iid,
                    taxonomy="us-gaap",
                    concept="Revenues",
                    unit="USD",
                    period_start=date(2009, 3, 29),
                    period_end=date(2009, 6, 27),
                    fiscal_year=2009,
                    fiscal_period=FiscalPeriod.Q3,
                    form="10-Q",
                    value=Decimal("8337000000"),
                    filed_at=date(2009, 7, 22),
                    available_at=US.next_session_open(date(2009, 7, 22)),
                    accession="0001193125-09-153165",
                    source=FundamentalSource.SEC,
                ),
                FundamentalRow(
                    instrument_id=iid,
                    taxonomy=FY2008.taxonomy,
                    concept=FY2008.concept,
                    unit=FY2008.unit,
                    period_start=FY2008.period_start,
                    period_end=FY2008.period_end,
                    fiscal_year=2009,
                    fiscal_period=FiscalPeriod.FY,
                    form="10-K",
                    value=Decimal("5.48"),
                    filed_at=date(2009, 10, 27),
                    available_at=US.next_session_open(date(2009, 10, 27)),
                    accession="0001193125-09-214859",
                    source=FundamentalSource.SEC,
                ),
            ],
        )
        s.commit()

        yield s, iid

        for table in ("fundamental", "filing", "instrument"):
            s.execute(text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": iid})
        s.commit()


def at(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 12, 0, tzinfo=UTC)


def look(session: Session, iid: int, when: datetime) -> repo.FactLookup:
    return repo.value_as_of(session, iid, FY2008, asof=when)


class TestWeakestClaims:
    def test_before_the_value_source_begins(self, company: tuple[Session, int]) -> None:
        session, iid = company
        result = look(session, iid, at(2007, 1, 1))

        assert result.outcome is FactOutcome.SOURCE_COVERAGE_UNAVAILABLE
        assert "says nothing about what the market knew" in result.explain()

    def test_source_covers_the_era_but_not_this_fact(self, company: tuple[Session, int]) -> None:
        """The state the earlier two-state design was missing.

        At 2009-08-01 the value source has begun, but carries nothing for
        FY2008. The report existed; we simply cannot read it.
        """
        session, iid = company
        result = look(session, iid, at(2009, 8, 1))

        assert result.outcome is FactOutcome.NO_OBSERVATION_IN_SOURCE
        assert result.outcome is not FactOutcome.NOT_YET_FILED

    def test_it_names_the_filing_that_proves_the_market_had_it(
        self, company: tuple[Session, int]
    ) -> None:
        session, iid = company
        result = look(session, iid, at(2009, 8, 1))

        assert result.covering_filing is not None
        assert result.covering_filing.filed_at == date(2008, 11, 5)
        assert "the market had it and we cannot read it" in result.explain()

    def test_the_value_is_returned_once_it_is_tagged(self, company: tuple[Session, int]) -> None:
        session, iid = company
        result = look(session, iid, at(2010, 1, 1))

        assert result.outcome is FactOutcome.FOUND
        assert result.value == Decimal("5.48")


class TestTheOnlyClaimAboutTheWorld:
    def test_not_yet_filed_needs_the_register_to_show_nothing(
        self, company: tuple[Session, int]
    ) -> None:
        """FY2009 ended 2009-09-26; its report came 2009-10-27.

        Asked on 2009-10-01 the register genuinely holds no covering report, so
        this is the one absence permitted to assert something about the world.
        """
        session, iid = company

        result = repo.value_as_of(session, iid, FY2009, asof=at(2009, 10, 1))

        assert result.outcome is FactOutcome.NOT_YET_FILED
        assert "no report covering this period had been filed" in result.explain()

    def test_it_softens_once_the_report_is_available(self, company: tuple[Session, int]) -> None:
        """The same question after the report exists must stop claiming that."""
        session, iid = company

        result = repo.value_as_of(session, iid, FY2009, asof=at(2009, 11, 1))

        assert result.outcome is FactOutcome.NO_OBSERVATION_IN_SOURCE
        assert result.covering_filing is not None

    def test_an_empty_register_never_claims_non_publication(self, engine: object) -> None:
        """With no register there is no evidence, so no claim is permitted."""
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
        with factory() as s:
            inst = Instrument(market=Market.US, name="NO REGISTER CORP", us_cik="9999999996")
            s.add(inst)
            s.flush()
            iid = inst.instrument_id

            repo.save_facts(
                s,
                [
                    FundamentalRow(
                        instrument_id=iid,
                        taxonomy=FY2008.taxonomy,
                        concept=FY2008.concept,
                        unit=FY2008.unit,
                        period_start=FY2008.period_start,
                        period_end=FY2008.period_end,
                        fiscal_year=2009,
                        fiscal_period=FiscalPeriod.FY,
                        form="10-K",
                        value=Decimal("5.48"),
                        filed_at=date(2009, 10, 27),
                        available_at=US.next_session_open(date(2009, 10, 27)),
                        accession="acc-no-register",
                        source=FundamentalSource.SEC,
                    )
                ],
            )
            s.commit()

            result = repo.value_as_of(s, iid, FY2009, asof=at(2010, 1, 1))

            assert result.outcome is FactOutcome.NO_OBSERVATION_IN_SOURCE

            for table in ("fundamental", "instrument"):
                s.execute(text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": iid})
            s.commit()


class TestTransactionTimeIsNotBypassable:
    """`latest_value_as_of` must honour `ingested_before` like `value_as_of`.

    It is the convenience helper a factor engine reaches for, so a missing
    filter here would let the whole transaction-time axis be walked around by
    the most-used call in the system: a filing backfilled later carries an
    earlier filing date, passes `available_at <= asof`, and silently changes a
    backtest that ran before the backfill existed.
    """

    @staticmethod
    def seed(session: Session, iid: int) -> None:
        repo.save_facts(
            session,
            [
                FundamentalRow(
                    instrument_id=iid,
                    taxonomy="us-gaap",
                    concept="Revenues",
                    unit="USD",
                    period_start=date(2008, 9, 28),
                    period_end=date(2009, 9, 26),
                    fiscal_year=2009,
                    fiscal_period=FiscalPeriod.FY,
                    form="10-K",
                    value=Decimal("42905000000"),
                    filed_at=date(2009, 10, 27),
                    available_at=US.next_session_open(date(2009, 10, 27)),
                    accession="backfilled-later",
                    source=FundamentalSource.SEC,
                )
            ],
        )
        session.commit()
        # Stand in for the row arriving long after the filing date.
        session.execute(
            text("UPDATE fundamental SET ingested_at = :w WHERE accession = 'backfilled-later'"),
            {"w": datetime(2026, 1, 1, tzinfo=UTC)},
        )
        session.commit()

    def test_a_later_backfill_is_hidden_from_an_earlier_snapshot(
        self, company: tuple[Session, int]
    ) -> None:
        session, iid = company
        self.seed(session, iid)

        reproduced = repo.latest_value_as_of(
            session,
            iid,
            concept="Revenues",
            unit="USD",
            asof=at(2010, 1, 1),
            months=12,
            ingested_before=datetime(2025, 1, 1, tzinfo=UTC),
        )

        assert reproduced.outcome is not FactOutcome.FOUND, (
            "a row ingested in 2026 must not appear in a snapshot taken in 2025"
        )

    def test_without_the_bound_it_is_visible(self, company: tuple[Session, int]) -> None:
        session, iid = company
        self.seed(session, iid)

        live = repo.latest_value_as_of(
            session, iid, concept="Revenues", unit="USD", asof=at(2010, 1, 1), months=12
        )

        assert live.value == Decimal("42905000000")

    def test_source_can_be_restricted(self, company: tuple[Session, int]) -> None:
        """A historical backtest should ask for SEC only.

        The yfinance fallback carries no filing dates and cannot support a
        point-in-time claim, so it must be excludable.
        """
        session, iid = company
        self.seed(session, iid)

        from_sec = repo.latest_value_as_of(
            session,
            iid,
            concept="Revenues",
            unit="USD",
            asof=at(2010, 1, 1),
            months=12,
            source=FundamentalSource.SEC,
        )
        from_yfinance = repo.latest_value_as_of(
            session,
            iid,
            concept="Revenues",
            unit="USD",
            asof=at(2010, 1, 1),
            months=12,
            source=FundamentalSource.YFINANCE,
        )

        assert from_sec.value == Decimal("42905000000")
        assert from_yfinance.outcome is not FactOutcome.FOUND


class TestFormFamily:
    def test_an_amendment_counts_as_its_parent_report(self) -> None:
        """10-K/A is a correction to a 10-K, not a different kind of report."""
        assert filing_repo.form_family("10-K/A") == "10-K"
        assert filing_repo.form_family("10-Q/A") == "10-Q"
        assert filing_repo.form_family("10-K") == "10-K"
