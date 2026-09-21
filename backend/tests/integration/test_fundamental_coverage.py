"""A run must be covered by financials for its whole length, not just at the edge.

The coverage-start check answers where a source's record begins. It says
nothing about whether that record continues, and the difference is the whole
problem: an instrument with 2019 and 2021 filings and nothing between passes a
start check for any period after 2019, and then every session in 2020 and 2021
anchors on the 2019 figures. No absence is reported, because each lookup asks
only which period was latest at that instant and 2019 truthfully was.

Kept in its own file with a short window on purpose. This is the cheap half of
the coverage story and belongs in the gate; the strategy-behaviour tests next
door walk two years of sessions and take minutes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtest import strategies
from app.backtest.engine import CostModel
from app.backtest.strategies import technical_fundamental
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.types import Interval
from app.engines.fundamental import REQUIRED_MONTHS
from app.models import Base, Instrument
from app.models.fundamental import FiscalPeriod, FundamentalSource
from app.repositories import candle_repo, fundamental_repo
from app.repositories.candle_repo import CandleRow
from app.services import backtest_service as svc
from app.services import fundamental_service
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

CIK = fake_cik(__name__)
US = MarketCalendar(Market.US)

# Short on purpose: the gate runs after the simulation, so every session in the
# window is paid for. A quarter is enough to straddle the gap below.
RUN_START, RUN_END = date(2021, 3, 1), date(2021, 6, 30)
SESSIONS = US.sessions_between(RUN_START, RUN_END)

ANNUAL: dict[str, Decimal] = {
    "Revenues": Decimal("400000000"),
    "NetIncomeLoss": Decimal("90000000"),
    "OperatingIncomeLoss": Decimal("110000000"),
    "EarningsPerShareBasic": Decimal("5.5"),
    "EarningsPerShareDiluted": Decimal("5.4"),
    "Assets": Decimal("800000000"),
    "Liabilities": Decimal("300000000"),
    "StockholdersEquity": Decimal("500000000"),
    "CashAndCashEquivalentsAtCarryingValue": Decimal("120000000"),
}


def _facts(iid: int, year: int) -> list[fundamental_repo.FundamentalRow]:
    """One year's annual filing, filed the following spring.

    The lag matters to what this file tests. A period's `period_end` and its
    `filed_at` are different axes, and here they land in different calendar
    years — which is exactly the case a coverage scan must not conflate.
    """
    ends = date(year, 12, 31)
    filed = date(year + 1, 3, 30)
    available = US.next_session_open(filed)
    return [
        fundamental_repo.FundamentalRow(
            instrument_id=iid,
            taxonomy="us-gaap",
            concept=concept,
            unit=fundamental_service.unit_for(concept, "USD"),
            period_start=None if REQUIRED_MONTHS[concept] is None else date(year, 1, 1),
            period_end=ends,
            fiscal_year=year,
            fiscal_period=FiscalPeriod.FY,
            form="10-K",
            value=value,
            filed_at=filed,
            available_at=available,
            accession=f"{CIK}-{year}-FY",
            source=FundamentalSource.SEC,
        )
        for concept, value in ANNUAL.items()
    ]


@pytest.fixture(scope="module")
def db() -> Iterator[object]:
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
def instrument(db: object) -> Iterator[tuple[Session, Instrument]]:
    factory = sessionmaker(bind=db, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.US, name="COVERAGE TEST CORP", us_cik=CIK)
        s.add(inst)
        s.flush()

        candle_repo.save_revisions(
            s,
            [
                CandleRow(
                    instrument_id=inst.instrument_id,
                    interval=Interval.DAY_1,
                    ts=US.session_open(day),
                    available_at=US.session_close(day),
                    open=Decimal(100 + i),
                    high=Decimal(100 + i),
                    low=Decimal(100 + i),
                    close=Decimal(100 + i),
                    volume=Decimal("1000"),
                    source="TEST",
                )
                for i, day in enumerate(SESSIONS)
            ],
        )
        s.commit()

        yield s, inst

        s.execute(
            text("DELETE FROM backtest_run WHERE instrument_id = :i"),
            {"i": inst.instrument_id},
        )
        for table in ("fundamental", "candle", "instrument"):
            s.execute(
                text(f"DELETE FROM {table} WHERE instrument_id = :i"),
                {"i": inst.instrument_id},
            )
        s.commit()


def run(s: Session, inst: Instrument) -> object:
    return svc.execute(
        s,
        strategies.build(technical_fundamental(currency="USD")),
        svc.RunRequest(
            instrument_id=inst.instrument_id,
            start=RUN_START,
            end=RUN_END,
            starting_cash=Decimal("100000"),
            costs=CostModel(Decimal("5"), Decimal("5")),
        ),
    )


class TestAYearMissingFromTheMiddle:
    def test_it_is_refused(self, instrument: tuple[Session, Instrument]) -> None:
        """2019 and 2021 filed, 2020 absent, and the run sits across the hole.

        Every session would anchor on 2019 — eighteen months stale by the end
        of the window, and presented as the current picture.

        The left edge of the gap is also the earliest filing this instrument
        has, and its `period_end` (2019-12-31) precedes its `filed_at`
        (2020-03-30). A scan that begins at the coverage start — a filing date —
        and compares it against `period_end` drops that period entirely, and
        with it the only pair that reveals the gap. Samsung's real filings have
        this shape: 2013, 2014 and 2015 were all first filed on 2016-03-30 as
        comparatives in one report, so three periods sit below the earliest
        filing date.
        """
        s, inst = instrument
        fundamental_repo.save_facts(s, _facts(inst.instrument_id, 2019))
        fundamental_repo.save_facts(s, _facts(inst.instrument_id, 2021))
        s.flush()

        with pytest.raises(svc.BacktestWindowError, match="missing annual"):
            run(s, inst)

    def test_an_unbroken_record_is_not(self, instrument: tuple[Session, Instrument]) -> None:
        """The same window with 2020 present must run, or the check is just a
        refusal of everything."""
        s, inst = instrument
        for year in (2019, 2020, 2021):
            fundamental_repo.save_facts(s, _facts(inst.instrument_id, year))
        s.flush()

        assert run(s, inst) is not None

    def test_a_gap_outside_the_run_is_not_refused(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        """A hole the simulation never reaches changes nothing it measures."""
        s, inst = instrument
        for year in (2014, 2016, 2019, 2020, 2021):
            fundamental_repo.save_facts(s, _facts(inst.instrument_id, year))
        s.flush()

        assert run(s, inst) is not None


class TestTheScanReadsOneAxis:
    def test_periods_below_the_earliest_filing_still_count(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        """Stated directly, without going through a backtest.

        `coverage_start` is a filing date and `period_end` is a fiscal date.
        Filtering one by the other silently drops the periods whose reports
        landed in a later calendar year, which is most first filings and all
        comparatives.
        """
        s, inst = instrument
        fundamental_repo.save_facts(s, _facts(inst.instrument_id, 2019))
        fundamental_repo.save_facts(s, _facts(inst.instrument_id, 2021))
        s.flush()

        begins = fundamental_repo.coverage_start(
            s, inst.instrument_id, source=FundamentalSource.SEC
        )
        gaps = fundamental_repo.annual_gaps(s, inst.instrument_id, source=FundamentalSource.SEC)

        assert begins == date(2020, 3, 30), "the earliest filing postdates the earliest period"
        assert [(g.after, g.before) for g in gaps] == [(date(2019, 12, 31), date(2021, 12, 31))]
