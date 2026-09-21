"""A reproduced run must reproduce its reasons, not only its values.

`value_as_of` filtered facts by `ingested_before`, but the path taken when no
fact is found did not. `coverage_start`, `register_start` and
`covering_report_exists` all read whatever the tables hold now, so a filing
backfilled after a snapshot was taken would change how an old run classified an
absence.

The value stayed missing either way, which is why this was easy to miss. What
changed was the reason — and a backtest that reports different provenance for
the same question at the same snapshot has not been reproduced. Worse, the
observed direction was toward over-claiming: before the backfill the system
correctly declined to judge, and afterwards it asserted NOT_YET_FILED.

These tests pin all three classification inputs to the snapshot.
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
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

CIK = fake_cik(__name__)

US = MarketCalendar(Market.US)

SNAPSHOT = datetime(2025, 1, 1, tzinfo=UTC)
LATER = datetime(2026, 1, 1, tzinfo=UTC)
ASOF = datetime(2009, 10, 1, tzinfo=UTC)

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
    """One fact held since 2024, so value coverage begins before the question."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.US, name="REPRO TEST CORP", us_cik=CIK)
        s.add(inst)
        s.flush()
        iid = inst.instrument_id

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
                    accession="held-since-2024",
                    source=FundamentalSource.SEC,
                )
            ],
        )
        s.commit()
        _stamp_fact(s, "held-since-2024", datetime(2024, 1, 1, tzinfo=UTC))

        yield s, iid

        for table in ("fundamental", "filing", "instrument"):
            s.execute(text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": iid})
        s.commit()


def _stamp_fact(session: Session, accession: str, when: datetime) -> None:
    session.execute(
        text("UPDATE fundamental SET ingested_at = :w WHERE accession = :a"),
        {"w": when, "a": accession},
    )
    session.commit()


def _stamp_filing(session: Session, accession: str, when: datetime) -> None:
    session.execute(
        text("UPDATE filing SET ingested_at = :w WHERE accession = :a"),
        {"w": when, "a": accession},
    )
    session.commit()


def backfill_filing(session: Session, iid: int, accession: str) -> None:
    """A filing that existed in 2009 but only reached our register in 2026."""
    filing_repo.save_filings(
        session,
        [
            FilingRow(
                instrument_id=iid,
                form="10-K",
                filed_at=date(2009, 9, 30),
                period_of_report=date(2009, 9, 26),
                available_at=US.next_session_open(date(2009, 9, 30)),
                accession=accession,
                source=FundamentalSource.SEC,
            )
        ],
    )
    session.commit()
    _stamp_filing(session, accession, LATER)


class TestFilingRegisterRespectsTheSnapshot:
    def test_a_later_backfill_does_not_change_an_old_classification(
        self, company: tuple[Session, int]
    ) -> None:
        """The regression, stated in one assertion."""
        session, iid = company

        before = repo.value_as_of(session, iid, FY2009, asof=ASOF, ingested_before=SNAPSHOT)
        backfill_filing(session, iid, "backfilled-after-snapshot")
        after = repo.value_as_of(session, iid, FY2009, asof=ASOF, ingested_before=SNAPSHOT)

        assert before.outcome is after.outcome, (
            "the same question at the same snapshot must classify identically, "
            "however the register has grown since"
        )

    def test_the_backfill_did_change_the_unbounded_view(self, company: tuple[Session, int]) -> None:
        """Otherwise the previous test could pass by the filter doing nothing."""
        session, iid = company

        snapshotted = repo.value_as_of(session, iid, FY2009, asof=ASOF, ingested_before=SNAPSHOT)
        backfill_filing(session, iid, "backfilled-visible-live")
        live = repo.value_as_of(session, iid, FY2009, asof=ASOF)

        assert live.outcome is not snapshotted.outcome
        assert live.outcome is FactOutcome.NOT_YET_FILED

    def test_a_filing_the_register_held_then_is_still_seen(
        self, company: tuple[Session, int]
    ) -> None:
        """The bound must hide only what arrived later, not everything."""
        session, iid = company

        filing_repo.save_filings(
            session,
            [
                FilingRow(
                    instrument_id=iid,
                    form="10-K",
                    filed_at=date(2008, 11, 5),
                    period_of_report=date(2008, 9, 27),
                    available_at=US.next_session_open(date(2008, 11, 5)),
                    accession="held-since-2023",
                    source=FundamentalSource.SEC,
                )
            ],
        )
        session.commit()
        _stamp_filing(session, "held-since-2023", datetime(2023, 1, 1, tzinfo=UTC))

        fy2008 = FundamentalContext(
            taxonomy="us-gaap",
            concept="EarningsPerShareBasic",
            unit="USD/shares",
            period_end=date(2008, 9, 27),
            period_start=date(2007, 9, 30),
        )
        result = repo.value_as_of(
            session, iid, fy2008, asof=datetime(2009, 8, 1, tzinfo=UTC), ingested_before=SNAPSHOT
        )

        assert result.outcome is FactOutcome.NO_OBSERVATION_IN_SOURCE
        assert result.covering_filing is not None
        assert result.covering_filing.filed_at == date(2008, 11, 5)


class TestCoverageBoundaryRespectsTheSnapshot:
    def test_a_backfilled_fact_does_not_move_the_coverage_start(
        self, company: tuple[Session, int]
    ) -> None:
        """`coverage_start` is collected data too.

        A fact backfilled later carries an older filing date and would pull the
        apparent coverage boundary backwards, changing which absences count as
        "before this source begins".
        """
        session, iid = company

        before = repo.coverage_start(session, iid, ingested_before=SNAPSHOT)

        repo.save_facts(
            session,
            [
                FundamentalRow(
                    instrument_id=iid,
                    taxonomy="us-gaap",
                    concept="Revenues",
                    unit="USD",
                    period_start=date(2008, 9, 28),
                    period_end=date(2009, 3, 28),
                    fiscal_year=2009,
                    fiscal_period=FiscalPeriod.Q2,
                    form="10-Q",
                    value=Decimal("1"),
                    filed_at=date(2009, 4, 23),  # earlier than the held fact
                    available_at=US.next_session_open(date(2009, 4, 23)),
                    accession="backfilled-earlier-fact",
                    source=FundamentalSource.SEC,
                )
            ],
        )
        session.commit()
        _stamp_fact(session, "backfilled-earlier-fact", LATER)

        after = repo.coverage_start(session, iid, ingested_before=SNAPSHOT)
        unbounded = repo.coverage_start(session, iid)

        assert before == after == date(2009, 7, 22)
        assert unbounded == date(2009, 4, 23), "the live view should see it"

    def test_register_start_is_bounded_too(self, company: tuple[Session, int]) -> None:
        session, iid = company

        backfill_filing(session, iid, "register-start-backfill")

        assert filing_repo.register_start(session, iid, ingested_before=SNAPSHOT) is None
        assert filing_repo.register_start(session, iid) == date(2009, 9, 30)


class TestLatestHelperPassesTheBoundOn:
    def test_absence_classification_is_bounded_through_the_helper(
        self, company: tuple[Session, int]
    ) -> None:
        """`latest_value_as_of` is the call an engine makes, so it must carry it."""
        session, iid = company

        before = repo.latest_value_as_of(
            session,
            iid,
            concept="EarningsPerShareBasic",
            unit="USD/shares",
            asof=ASOF,
            months=12,
            ingested_before=SNAPSHOT,
        )
        backfill_filing(session, iid, "helper-path-backfill")
        after = repo.latest_value_as_of(
            session,
            iid,
            concept="EarningsPerShareBasic",
            unit="USD/shares",
            asof=ASOF,
            months=12,
            ingested_before=SNAPSHOT,
        )

        assert before.outcome is after.outcome
