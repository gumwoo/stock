"""Point-in-time reconstruction of financial facts.

An earlier version of this file asserted a timeline that never happened. It
read SEC's companyfacts, saw the earliest FY2008 EPS record dated 2009-10-27,
and concluded that a backtest in early 2009 would have had no figure. That is
false: Apple filed its FY2008 10-K on 2008-11-05 and the market knew EPS was
5.48 from that date. What companyfacts actually shows is where *XBRL tagging*
begins — phased in from June 2009, not applied retrospectively — and Apple's
earliest fact of any kind is filed 2009-07-22.

Mistaking a source's coverage boundary for the market's ignorance is the exact
error this project exists to avoid, so the distinction is now a return value
rather than an absent row.

The second correction: the restatement was excluded by the collector's own form
filter. Apple restated FY2008 EPS to 6.94 in a **10-K/A filed 2010-01-25**, and
`WANTED_FORMS` listed only unamended forms, so the restatement appeared to
arrive with the next annual 10-K in October — nine months late.

The real history, as SEC records it:

    2009-07-22   XBRL coverage for Apple begins
    2009-10-27   10-K     FY2008 EPS tagged at 5.48
    2010-01-25   10-K/A   restated to 6.94
    2010-10-27   10-K     6.94 carried forward
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
from app.repositories.fundamental_repo import (
    FactOutcome,
    FundamentalContext,
    FundamentalRow,
    RevisionPolicy,
)
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

CIK = fake_cik(__name__)

US = MarketCalendar(Market.US)

FY2008 = FundamentalContext(
    taxonomy="us-gaap",
    concept="EarningsPerShareBasic",
    unit="USD/shares",
    period_end=date(2008, 9, 27),
    period_start=date(2007, 9, 30),
)

# The three genuine filings that touch FY2008 basic EPS.
FILINGS = [
    (date(2009, 10, 27), "10-K", Decimal("5.48"), "0001193125-09-214859"),
    (date(2010, 1, 25), "10-K/A", Decimal("6.94"), "0001193125-10-012091"),
    (date(2010, 10, 27), "10-K", Decimal("6.94"), "0001193125-10-238044"),
]


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
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.US, name="PIT TEST CORP", us_cik=CIK)
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
                    fiscal_year=filed.year,
                    fiscal_period=FiscalPeriod.FY,
                    form=form,
                    value=value,
                    filed_at=filed,
                    available_at=US.next_session_open(filed),
                    accession=accn,
                    source=FundamentalSource.SEC,
                )
                for filed, form, value, accn in FILINGS
            ],
        )
        s.commit()

        yield s, iid

        s.execute(text("DELETE FROM fundamental WHERE instrument_id = :i"), {"i": iid})
        s.execute(text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": iid})
        s.commit()


def lookup(
    session: Session,
    iid: int,
    when: datetime,
    policy: RevisionPolicy = RevisionPolicy.AS_KNOWN_THEN,
) -> repo.FactLookup:
    return repo.value_as_of(session, iid, FY2008, asof=when, policy=policy)


def at(y: int, m: int = 1, d: int = 1) -> datetime:
    """Noon UTC — comfortably clear of any US session boundary for year-scale
    assertions. Day-scale assertions use `at_open` instead, because noon UTC is
    07:00 ET and the market has not opened yet."""
    return datetime(y, m, d, 12, 0, tzinfo=UTC)


def at_open(d: date) -> datetime:
    """The instant the US session on `d` opens."""
    return US.session_open(d)


class TestTheRealTimeline:
    def test_before_coverage_is_not_reported_as_unfiled(self, apple: tuple[Session, int]) -> None:
        """The correction that matters most.

        Apple's FY2008 10-K was filed 2008-11-05 and the market knew 5.48 from
        then. Our source simply does not reach that far back. Saying "not filed
        yet" would assert something false about the market.
        """
        session, iid = apple

        result = lookup(session, iid, at(2009, 1, 1))

        assert result.outcome is FactOutcome.SOURCE_COVERAGE_UNAVAILABLE
        assert result.outcome is not FactOutcome.NOT_YET_FILED
        assert result.coverage_start == date(2009, 10, 27)
        assert "says nothing about what the market knew" in result.explain()

    def test_the_originally_tagged_figure(self, apple: tuple[Session, int]) -> None:
        session, iid = apple
        result = lookup(session, iid, at(2010, 1, 1))

        assert result.value == Decimal("5.48")
        assert result.fact is not None
        assert result.fact.form == "10-K"

    def test_the_restatement_lands_with_the_amendment_in_january(
        self, apple: tuple[Session, int]
    ) -> None:
        """Not with the following October's 10-K.

        This is what the collector's form filter got wrong: excluding 10-K/A
        delayed the apparent publication of the restatement by nine months.
        """
        session, iid = apple

        result = lookup(session, iid, at_open(date(2010, 1, 26)))

        assert result.value == Decimal("6.94")
        assert result.fact is not None
        assert result.fact.form == "10-K/A"
        assert result.fact.filed_at == date(2010, 1, 25)

    def test_the_day_before_the_amendment_still_shows_the_old_figure(
        self, apple: tuple[Session, int]
    ) -> None:
        session, iid = apple
        assert lookup(session, iid, at(2010, 1, 24)).value == Decimal("5.48")

    def test_later_dates_keep_the_restated_figure(self, apple: tuple[Session, int]) -> None:
        session, iid = apple
        assert lookup(session, iid, at(2011, 1, 1)).value == Decimal("6.94")
        assert lookup(session, iid, at(2026, 1, 1)).value == Decimal("6.94")


class TestFirstObservedInSource:
    """Named for what it can promise, which is less than "as first reported"."""

    POLICY = RevisionPolicy.FIRST_OBSERVED_IN_SOURCE

    def test_it_pins_to_the_earliest_filing_the_source_carries(
        self, apple: tuple[Session, int]
    ) -> None:
        session, iid = apple

        for year in (2010, 2011, 2026):
            assert lookup(session, iid, at(year), self.POLICY).value == Decimal("5.48")

    def test_it_is_not_the_same_as_the_original_announcement(
        self, apple: tuple[Session, int]
    ) -> None:
        """Here they coincide in value but not in provenance.

        The earliest filing this source holds is the 2009 10-K, not the FY2008
        10-K of 2008-11-05 where the figure was actually first announced. The
        name reflects that limit rather than papering over it.
        """
        session, iid = apple

        result = lookup(session, iid, at(2011), self.POLICY)

        assert result.fact is not None
        assert result.fact.filed_at == date(2009, 10, 27)
        assert result.fact.filed_at > date(2008, 11, 5)

    def test_the_two_policies_diverge_after_the_amendment(self, apple: tuple[Session, int]) -> None:
        session, iid = apple

        known = lookup(session, iid, at(2011), RevisionPolicy.AS_KNOWN_THEN)
        first = lookup(session, iid, at(2011), self.POLICY)

        assert known.value == Decimal("6.94")
        assert first.value == Decimal("5.48")


class TestAvailability:
    def test_a_filing_is_not_usable_on_its_own_filing_date(
        self, apple: tuple[Session, int]
    ) -> None:
        """SEC gives a filing date with no time of day.

        It cannot distinguish a 06:00 dissemination from a 14:00 one, and under
        Regulation S-T Rule 13 anything transmitted after 17:30 ET is deemed
        filed the next business day anyway. So the boundary is the next
        session's open — the amendment filed Monday is usable from Tuesday's
        open, not from Tuesday midnight.
        """
        session, iid = apple
        filed_on = date(2010, 1, 25)  # Monday

        during_filing_day = lookup(session, iid, at(2010, 1, 25))
        next_day_premarket = lookup(session, iid, at(2010, 1, 26))  # 07:00 ET
        next_day_open = lookup(session, iid, at_open(date(2010, 1, 26)))

        assert during_filing_day.value == Decimal("5.48")
        assert next_day_premarket.value == Decimal("5.48"), (
            "pre-market is still before the availability boundary"
        )
        assert next_day_open.value == Decimal("6.94")
        assert next_day_open.fact is not None
        assert next_day_open.fact.available_at == US.next_session_open(filed_on)


class TestPeriodStartIsPartOfIdentity:
    """A quarterly figure and a year-to-date figure are not the same series.

    Apple's filings contain 92 contexts where `period_start` is the only
    difference: a 10-Q dated 2026-07-31 reports $29.8bn for three months and
    $101.5bn for nine months, both ending 2026-06-27, same unit, same form,
    same filing. Selecting without it picks one by row id — a 3.4x error in
    whichever direction the database happens to order rows.
    """

    QUARTER_END = date(2026, 6, 27)
    FILED = date(2026, 7, 31)

    @staticmethod
    def seed(session: Session, iid: int) -> None:
        rows = [
            (date(2026, 3, 29), Decimal("29789000000")),  # 3 months
            (date(2025, 9, 28), Decimal("101464000000")),  # 9 months
        ]
        repo.save_facts(
            session,
            [
                FundamentalRow(
                    instrument_id=iid,
                    taxonomy="us-gaap",
                    concept="NetIncomeLoss",
                    unit="USD",
                    period_start=start,
                    period_end=TestPeriodStartIsPartOfIdentity.QUARTER_END,
                    fiscal_year=2026,
                    fiscal_period=FiscalPeriod.Q3,
                    form="10-Q",
                    value=value,
                    filed_at=TestPeriodStartIsPartOfIdentity.FILED,
                    available_at=US.next_session_open(TestPeriodStartIsPartOfIdentity.FILED),
                    accession="0000320193-26-000081",
                    source=FundamentalSource.SEC,
                )
                for start, value in rows
            ],
        )
        session.commit()

    def context(self, start: date) -> FundamentalContext:
        return FundamentalContext(
            taxonomy="us-gaap",
            concept="NetIncomeLoss",
            unit="USD",
            period_end=self.QUARTER_END,
            period_start=start,
        )

    def test_both_periods_are_stored_separately(self, apple: tuple[Session, int]) -> None:
        session, iid = apple
        self.seed(session, iid)

        quarter = repo.value_as_of(
            session, iid, self.context(date(2026, 3, 29)), asof=at(2026, 9, 1)
        )
        ytd = repo.value_as_of(session, iid, self.context(date(2025, 9, 28)), asof=at(2026, 9, 1))

        assert quarter.value == Decimal("29789000000")
        assert ytd.value == Decimal("101464000000")

    def test_they_never_substitute_for_one_another(self, apple: tuple[Session, int]) -> None:
        """The failure mode: a 3.4x error that raises nothing."""
        session, iid = apple
        self.seed(session, iid)

        quarter = repo.value_as_of(
            session, iid, self.context(date(2026, 3, 29)), asof=at(2026, 9, 1)
        )

        assert quarter.value != Decimal("101464000000")
        assert quarter.fact is not None
        assert quarter.fact.period_start == date(2026, 3, 29)

    def test_duration_selection_picks_the_right_window(self, apple: tuple[Session, int]) -> None:
        """`latest_value_as_of` must not mix durations when the caller asks."""
        session, iid = apple
        self.seed(session, iid)

        three = repo.latest_value_as_of(
            session, iid, concept="NetIncomeLoss", unit="USD", asof=at(2026, 9, 1), months=3
        )
        nine = repo.latest_value_as_of(
            session, iid, concept="NetIncomeLoss", unit="USD", asof=at(2026, 9, 1), months=9
        )

        assert three.value == Decimal("29789000000")
        assert nine.value == Decimal("101464000000")

    def test_revisions_are_scoped_to_one_duration(self, apple: tuple[Session, int]) -> None:
        session, iid = apple
        self.seed(session, iid)

        history = repo.revisions_of(session, iid, self.context(date(2026, 3, 29)))

        assert len(history) == 1
        assert history[0].value == Decimal("29789000000")


class TestIdempotence:
    def test_a_null_period_start_still_counts_as_a_duplicate(
        self, apple: tuple[Session, int]
    ) -> None:
        """Postgres treats NULLs as distinct in UNIQUE unless told otherwise.

        Instantaneous facts — balances, measured at a date rather than across a
        span — all carry NULL here, so without NULLS NOT DISTINCT they
        duplicated on every collection run.
        """
        session, iid = apple

        balance = FundamentalRow(
            instrument_id=iid,
            taxonomy="us-gaap",
            concept="Assets",
            unit="USD",
            period_start=None,
            period_end=date(2008, 9, 27),
            fiscal_year=2009,
            fiscal_period=FiscalPeriod.FY,
            form="10-K",
            value=Decimal("39572000000"),
            filed_at=date(2009, 10, 27),
            available_at=US.next_session_open(date(2009, 10, 27)),
            accession="0001193125-09-214859",
            source=FundamentalSource.SEC,
        )

        assert repo.save_facts(session, [balance]) == 1
        session.commit()
        assert repo.save_facts(session, [balance]) == 0
        session.commit()
