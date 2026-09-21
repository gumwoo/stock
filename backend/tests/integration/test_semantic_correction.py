"""Removing data a run already used, and what does and does not catch it.

Migration `c3e8a51d7f04` deleted the Korean `NetIncomeLoss` and
`StockholdersEquity` rows, because they had been filled from the including-NCI
IFRS tags and the us-gaap names they sat under mean the parent-only figures.
The values were wrong. Deleting them was still the wrong operation, and this
file pins both halves of why.

**The coverage gate does not refuse the transitional state.** Its job is to ask
whether the scorer can anchor, and `Revenues` and `EarningsPerShareBasic` were
never touched, so it can. The engine then degrades as designed: return on
equity drops out, four ratios remain, the profitability requirement is still
met by P/E, and a factor is produced. Measured on Samsung over ten years, the
same strategy returns +502.43% with the rows and +597.76% without them — a
95-point difference that looks like a better strategy and is a thinner dataset.

**What does catch it is reproduction, and only for a run already stored.** The
`ingested_at` axis was built so a later backfill cannot change an old result;
it can hide rows that arrived afterwards, and there is nothing it can do about
rows that stopped existing. So a stored run fails to reproduce — loudly, which
is the point — while a new run started in the same state is simply wrong and
says nothing.

The lesson is not a better gate. A gate cannot refuse an absence it has no
record of; after the delete there is nothing to say `NetIncomeLoss` was ever
expected. A semantic correction should relabel rather than delete, so the rows
keep saying what they actually are and the scorer stops reading them.
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
from app.services import reproduce_service as rs
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

CIK = fake_cik(__name__)
US = MarketCalendar(Market.US)

RUN_START, RUN_END = date(2021, 1, 4), date(2022, 6, 30)
SESSIONS = US.sessions_between(RUN_START, RUN_END)

# The two the migration removed, and the concepts that feed them.
REMOVED = ("NetIncomeLoss", "StockholdersEquity")

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
    filed = date(year + 1, 3, 30)
    return [
        fundamental_repo.FundamentalRow(
            instrument_id=iid,
            taxonomy="us-gaap",
            concept=concept,
            unit=fundamental_service.unit_for(concept, "USD"),
            period_start=None if REQUIRED_MONTHS[concept] is None else date(year, 1, 1),
            period_end=date(year, 12, 31),
            fiscal_year=year,
            fiscal_period=FiscalPeriod.FY,
            form="10-K",
            value=value,
            filed_at=filed,
            available_at=US.next_session_open(filed),
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
        inst = Instrument(market=Market.US, name="CORRECTION TEST CORP", us_cik=CIK)
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
        for year in (2018, 2019, 2020, 2021):
            fundamental_repo.save_facts(s, _facts(inst.instrument_id, year))
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


def request_for(iid: int) -> svc.RunRequest:
    return svc.RunRequest(
        instrument_id=iid,
        start=RUN_START,
        end=RUN_END,
        starting_cash=Decimal("100000"),
        costs=CostModel(Decimal("5"), Decimal("5")),
    )


def run(s: Session, inst: Instrument) -> svc.RunOutcome:
    return svc.execute(
        s, strategies.build(technical_fundamental(currency="USD")), request_for(inst.instrument_id)
    )


def remove_the_corrected_concepts(s: Session, iid: int) -> int:
    removed = s.execute(
        text(
            "DELETE FROM fundamental WHERE instrument_id = :i "
            "AND concept IN ('NetIncomeLoss', 'StockholdersEquity')"
        ),
        {"i": iid},
    ).rowcount
    s.flush()
    return removed


class TestTheGateDoesNotRefuseTheTransitionalState:
    """Pinned as a hazard, not as intended behaviour.

    The migration's note claimed the coverage gate would refuse until the data
    was recollected. It does not, and a claim about a safeguard that is not
    there is worse than no claim.
    """

    def test_a_run_still_executes_after_the_rows_are_removed(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        s, inst = instrument
        assert remove_the_corrected_concepts(s, inst.instrument_id) > 0

        assert run(s, inst) is not None, "the gate refuses; update the migration note"

    def test_the_anchor_survives_because_it_was_never_touched(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        """Why it is not refused: the gate asks whether the scorer can anchor,
        and revenue and EPS are still there."""
        s, inst = instrument
        remove_the_corrected_concepts(s, inst.instrument_id)

        ends = fundamental_repo.annual_period_ends(
            s,
            inst.instrument_id,
            concepts=fundamental_service.ANCHOR_CANDIDATES,
            source=FundamentalSource.SEC,
        )

        assert len(ends) == 4

    def test_the_result_moves(self, instrument: tuple[Session, Instrument]) -> None:
        """The reason this matters. Same strategy, same window, fewer inputs,
        a different number and nothing saying so.

        On Samsung over ten years the same removal moved the reported return
        from +502.43% to +597.76%.
        """
        s, inst = instrument
        before = run(s, inst).result

        remove_the_corrected_concepts(s, inst.instrument_id)
        after = run(s, inst).result

        assert [p.value for p in before.equity_curve] != [p.value for p in after.equity_curve]


class TestReproductionIsWhatCatchesIt:
    def test_a_stored_run_no_longer_reproduces(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        """The guard that does exist, and the only one that can.

        `ingested_at` keeps a later backfill from changing an old result. It
        can hide rows that arrived after the snapshot; it can do nothing about
        rows that stopped existing, because a filter cannot restore them. So
        the deletion is caught here rather than prevented earlier.
        """
        s, inst = instrument
        report = svc.walk_forward(
            s,
            svc.StrategySpec(definition=technical_fundamental(currency="USD")),
            request_for(inst.instrument_id),
            train_sessions=200,
            eval_sessions=60,
        )
        stored = svc.persist(s, report)
        s.commit()
        assert rs.reproduce(s, stored.id).reproduced

        remove_the_corrected_concepts(s, inst.instrument_id)
        s.commit()

        assert not rs.reproduce(s, stored.id).reproduced

    def test_the_snapshot_filter_cannot_hide_a_deletion(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        """Stated directly, because it is the reason deleting is different in
        kind from every other change to this table."""
        s, inst = instrument
        snapshot = svc.snapshot_now(s)
        before = fundamental_repo.annual_period_ends(
            s,
            inst.instrument_id,
            concepts=REMOVED,
            source=FundamentalSource.SEC,
            ingested_before=snapshot,
        )

        remove_the_corrected_concepts(s, inst.instrument_id)

        after = fundamental_repo.annual_period_ends(
            s,
            inst.instrument_id,
            concepts=REMOVED,
            source=FundamentalSource.SEC,
            ingested_before=snapshot,
        )

        assert before and not after
