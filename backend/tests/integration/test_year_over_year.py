"""Year-on-year growth steps back a fiscal period, not 365 days.

Subtracting a year from `asof` and asking what was latest then looks equivalent
to asking for the previous fiscal year. It is not, because it conflates when a
period ended with when its report happened to become readable, and both ways of
being wrong were observable in Apple's real filings.

**The period can slip.** Apple's FY2024 report became usable on 2024-11-04.
Scoring on 2025-11-03 put `asof - 365` at 2024-11-03 — one day short — so the
lookup fell through to FY2023 and reported a 728-day change as year-on-year
growth: +8.6% where the real figure was +6.4%. Neither number looks wrong.

**Restatements get hidden.** Asking as of a year ago also refuses to see any
revision published since. Apple restated FY2024 revenue in the FY2025 10-K
filed 2025-10-31, and under AS_KNOWN_THEN a scorer running in November 2025
should use that figure, because the market had it.

The dates below are Apple's actual filing dates.
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
    FundamentalRow,
    RevisionPolicy,
)
from app.services import fundamental_service as service
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

CIK = fake_cik(__name__)

US = MarketCalendar(Market.US)
CONCEPT = "RevenueFromContractWithCustomerExcludingAssessedTax"

# (period_start, period_end, value, filed_at, accession)
FILINGS = [
    (date(2022, 10, 2), date(2023, 9, 30), "383285000000", date(2023, 11, 3), "fy2023"),
    (date(2023, 10, 1), date(2024, 9, 28), "391035000000", date(2024, 11, 1), "fy2024"),
    (date(2024, 9, 29), date(2025, 9, 27), "416161000000", date(2025, 10, 31), "fy2025"),
    # FY2024 restated as a comparative in the FY2025 annual report.
    (date(2023, 10, 1), date(2024, 9, 28), "391100000000", date(2025, 10, 31), "fy2024-restated"),
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
def company(engine: object) -> Iterator[tuple[Session, int]]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.US, name="YOY TEST CORP", us_cik=CIK)
        s.add(inst)
        s.flush()
        iid = inst.instrument_id

        repo.save_facts(
            s,
            [
                FundamentalRow(
                    instrument_id=iid,
                    taxonomy="us-gaap",
                    concept=CONCEPT,
                    unit="USD",
                    period_start=start,
                    period_end=end,
                    fiscal_year=end.year,
                    fiscal_period=FiscalPeriod.FY,
                    form="10-K",
                    value=Decimal(amount),
                    filed_at=filed,
                    available_at=US.next_session_open(filed),
                    accession=accn,
                    source=FundamentalSource.SEC,
                )
                for start, end, amount, filed, accn in FILINGS
            ],
        )
        s.commit()

        yield s, iid

        s.execute(text("DELETE FROM fundamental WHERE instrument_id = :i"), {"i": iid})
        s.execute(text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": iid})
        s.commit()


def prior(session: Session, iid: int, when: datetime) -> repo.FactLookup:
    return repo.previous_annual_fact(
        session,
        iid,
        concept=CONCEPT,
        unit="USD",
        before_period_end=date(2025, 9, 27),
        asof=when,
        policy=RevisionPolicy.AS_KNOWN_THEN,
    )


class TestThePeriodDoesNotSlip:
    def test_the_boundary_day_still_finds_the_prior_year(
        self, company: tuple[Session, int]
    ) -> None:
        """The day a 365-day subtraction fell one short.

        FY2024 became usable 2024-11-04. At asof 2025-11-03 the old approach
        looked at 2024-11-03 and took FY2023 instead.
        """
        session, iid = company

        result = prior(session, iid, datetime(2025, 11, 3, 15, 0, tzinfo=UTC))

        assert result.fact is not None
        assert result.fact.period_end == date(2024, 9, 28)
        assert result.fact.period_end != date(2023, 9, 30)

    def test_the_gap_is_one_year_not_two(self, company: tuple[Session, int]) -> None:
        session, iid = company

        result = prior(session, iid, datetime(2025, 11, 3, 15, 0, tzinfo=UTC))

        assert result.fact is not None
        gap = (date(2025, 9, 27) - result.fact.period_end).days
        assert gap < 400, f"{gap} days is not a year-on-year comparison"

    @pytest.mark.parametrize("day", [3, 4, 10, 30])
    def test_it_is_stable_across_nearby_dates(self, company: tuple[Session, int], day: int) -> None:
        """The answer must not depend on which day of November it is asked."""
        session, iid = company

        result = prior(session, iid, datetime(2025, 11, day, 15, 0, tzinfo=UTC))

        assert result.fact is not None
        assert result.fact.period_end == date(2024, 9, 28)


class TestRestatementsAreVisible:
    def test_the_revision_is_chosen_at_the_current_asof(self, company: tuple[Session, int]) -> None:
        """FY2024 was restated in the FY2025 10-K; the market had that figure."""
        session, iid = company

        result = prior(session, iid, datetime(2025, 11, 10, 15, 0, tzinfo=UTC))

        assert result.fact is not None
        assert result.fact.filed_at == date(2025, 10, 31)
        assert result.value == Decimal("391100000000")

    def test_before_the_restatement_the_original_stands(self, company: tuple[Session, int]) -> None:
        """Stepping by period must not leak the future revision backwards."""
        session, iid = company

        result = repo.previous_annual_fact(
            session,
            iid,
            concept=CONCEPT,
            unit="USD",
            before_period_end=date(2025, 9, 27),
            asof=datetime(2025, 6, 1, 15, 0, tzinfo=UTC),
            policy=RevisionPolicy.AS_KNOWN_THEN,
        )

        assert result.fact is not None
        assert result.fact.filed_at == date(2024, 11, 1)
        assert result.value == Decimal("391035000000")

    def test_first_observed_policy_keeps_the_original(self, company: tuple[Session, int]) -> None:
        session, iid = company

        result = repo.previous_annual_fact(
            session,
            iid,
            concept=CONCEPT,
            unit="USD",
            before_period_end=date(2025, 9, 27),
            asof=datetime(2025, 11, 10, 15, 0, tzinfo=UTC),
            policy=RevisionPolicy.FIRST_OBSERVED_IN_SOURCE,
        )

        assert result.value == Decimal("391035000000")


class TestThroughTheSnapshot:
    def test_growth_is_year_on_year_on_the_boundary_day(self, company: tuple[Session, int]) -> None:
        """End to end: the figure a user would actually see."""
        session, iid = company

        snapshot = service.build_snapshot(
            session,
            iid,
            asof=datetime(2025, 11, 3, 15, 0, tzinfo=UTC),
            price=250.0,
            currency="USD",
            source=FundamentalSource.SEC,
        )

        current = snapshot.revenue()
        previous = snapshot.prior_revenue()

        assert current is not None and previous is not None
        growth = (current - previous) / previous * 100
        # Against FY2023 this read +8.6%, a two-year change labelled annual.
        assert growth == pytest.approx(6.4, abs=0.2)

    def test_no_prior_year_is_reported_as_absent_not_zero(
        self, company: tuple[Session, int]
    ) -> None:
        """At the start of coverage there is genuinely nothing to compare to."""
        session, iid = company

        result = repo.previous_annual_fact(
            session,
            iid,
            concept=CONCEPT,
            unit="USD",
            before_period_end=date(2023, 9, 30),
            asof=datetime(2023, 12, 1, 15, 0, tzinfo=UTC),
            policy=RevisionPolicy.AS_KNOWN_THEN,
        )

        assert result.fact is None
        assert result.value is None


class TestAdjacencyIsChecked:
    """A missing intervening year must not become a silent two-year growth.

    "The largest period_end below this one" is not the same as "the year
    before". When the intervening year is simply absent from the source, that
    phrasing reaches two years back and produces exactly the comparison this
    function exists to prevent — the earlier 365-day bug arriving through a
    different door.
    """

    @staticmethod
    def drop_fy2024(session: Session, iid: int) -> None:
        session.execute(
            text("DELETE FROM fundamental WHERE instrument_id = :i AND period_end = :p"),
            {"i": iid, "p": date(2024, 9, 28)},
        )
        session.commit()

    def test_a_missing_year_yields_absence_not_the_year_before_it(
        self, company: tuple[Session, int]
    ) -> None:
        session, iid = company
        self.drop_fy2024(session, iid)

        result = prior(session, iid, datetime(2025, 11, 10, 15, 0, tzinfo=UTC))

        assert result.fact is None, (
            "FY2023 is two years back; returning it would label a 728-day "
            "change as year-on-year growth"
        )
        assert result.outcome is not FactOutcome.FOUND

    def test_the_snapshot_reports_no_growth_rather_than_a_wrong_one(
        self, company: tuple[Session, int]
    ) -> None:
        """End to end: a user sees the metric absent, not a plausible lie."""
        session, iid = company
        self.drop_fy2024(session, iid)

        snapshot = service.build_snapshot(
            session,
            iid,
            asof=datetime(2025, 11, 10, 15, 0, tzinfo=UTC),
            price=250.0,
            currency="USD",
            source=FundamentalSource.SEC,
        )

        assert snapshot.prior_revenue() is None

    def test_an_adjacent_year_is_still_accepted(self, company: tuple[Session, int]) -> None:
        """The guard must not reject the normal case it sits beside."""
        session, iid = company

        result = prior(session, iid, datetime(2025, 11, 10, 15, 0, tzinfo=UTC))

        assert result.fact is not None
        assert result.fact.period_end == date(2024, 9, 28)

    def test_a_53_week_year_is_not_rejected(self, company: tuple[Session, int]) -> None:
        """Many filers run 52/53-week calendars, so the bound has slack."""
        session, iid = company

        result = repo.previous_annual_fact(
            session,
            iid,
            concept=CONCEPT,
            unit="USD",
            before_period_end=date(2025, 9, 27),
            asof=datetime(2025, 11, 10, 15, 0, tzinfo=UTC),
            max_gap_days=430,
        )

        assert result.fact is not None


class TestTheSnapshotBoundIsNotBypassed:
    """Every part of the lookup honours transaction time and source.

    An earlier version derived `period_end` and `period_start` in two separate
    queries, and the second carried none of the filters. A row backfilled after
    a snapshot, or one from a different provider, could supply the period the
    context was then built from — reopening a bypass closed elsewhere in the
    repository.
    """

    @staticmethod
    def backfill_an_earlier_year(session: Session, iid: int) -> None:
        repo.save_facts(
            session,
            [
                FundamentalRow(
                    instrument_id=iid,
                    taxonomy="us-gaap",
                    concept=CONCEPT,
                    unit="USD",
                    period_start=date(2021, 9, 26),
                    period_end=date(2022, 9, 24),
                    fiscal_year=2022,
                    fiscal_period=FiscalPeriod.FY,
                    form="10-K",
                    value=Decimal("394328000000"),
                    filed_at=date(2022, 10, 28),
                    available_at=US.next_session_open(date(2022, 10, 28)),
                    accession="fy2022-backfilled",
                    source=FundamentalSource.SEC,
                )
            ],
        )
        session.commit()
        session.execute(
            text("UPDATE fundamental SET ingested_at = :w WHERE accession = 'fy2022-backfilled'"),
            {"w": datetime(2026, 1, 1, tzinfo=UTC)},
        )
        session.commit()

    def test_a_backfilled_row_is_invisible_to_an_earlier_snapshot(
        self, company: tuple[Session, int]
    ) -> None:
        session, iid = company
        self.backfill_an_earlier_year(session, iid)

        result = repo.previous_annual_fact(
            session,
            iid,
            concept=CONCEPT,
            unit="USD",
            before_period_end=date(2023, 9, 30),
            asof=datetime(2023, 12, 1, 15, 0, tzinfo=UTC),
            ingested_before=datetime(2025, 1, 1, tzinfo=UTC),
        )

        assert result.fact is None

    def test_it_is_visible_without_the_bound(self, company: tuple[Session, int]) -> None:
        """Otherwise the previous test could pass by the filter doing nothing."""
        session, iid = company
        self.backfill_an_earlier_year(session, iid)

        result = repo.previous_annual_fact(
            session,
            iid,
            concept=CONCEPT,
            unit="USD",
            before_period_end=date(2023, 9, 30),
            asof=datetime(2023, 12, 1, 15, 0, tzinfo=UTC),
        )

        assert result.fact is not None
        assert result.fact.period_end == date(2022, 9, 24)

    def test_another_source_does_not_supply_the_period(self, company: tuple[Session, int]) -> None:
        """A historical backtest asking for SEC must not be handed a DART row."""
        session, iid = company

        result = repo.previous_annual_fact(
            session,
            iid,
            concept=CONCEPT,
            unit="USD",
            before_period_end=date(2025, 9, 27),
            asof=datetime(2025, 11, 10, 15, 0, tzinfo=UTC),
            source=FundamentalSource.DART,
        )

        assert result.fact is None
