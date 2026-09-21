"""A run re-executed must be the same run.

Two different claims, and only the first is easy.

**Determinism**: same inputs, same output. Any pure function passes this.

**Backfill reproducibility**: the same run, re-executed after the database has
grown, must still produce what it originally reported. This is the one that
fails quietly. A bar collected later carries its original timestamp, so it
satisfies every look-ahead check ever written; it simply was not there the
first time. Without `ingested_at <= data_snapshot_at` the re-run silently
becomes a different run wearing the same id, and the difference shows up as a
better number rather than an error.

The test plants the backfill in the middle of the simulated period — where it
changes trades, not merely the final mark — and asserts from both sides: the
snapshot-bounded run must not move, and the unbounded run must, or the test
would pass against a database where the backfill failed to land.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtest import engine as bt
from app.backtest.engine import CostModel
from app.backtest.pit_repository import PitReader, snapshot_now
from app.backtest.strategies import MovingAverageCross
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.types import Interval
from app.models import Base, Instrument
from app.repositories import candle_repo
from app.repositories.candle_repo import CandleRow

pytestmark = pytest.mark.integration

US = MarketCalendar(Market.US)

# Long enough for a 10/30 crossover to have something to cross.
PERIOD = US.sessions_between(date(2025, 1, 2), date(2025, 9, 30))
STRATEGY = MovingAverageCross(short=10, long=30)


def _row(iid: int, day: date, price: Decimal) -> CandleRow:
    return CandleRow(
        instrument_id=iid,
        interval=Interval.DAY_1,
        ts=US.session_open(day),
        available_at=US.session_close(day),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1000"),
        source="TEST",
    )


def _price(index: int) -> Decimal:
    """A rise, a fall and a rise — enough to make the averages cross twice."""
    if index < 60:
        return Decimal(100 + index)
    if index < 120:
        return Decimal(160 - (index - 60))
    return Decimal(100 + (index - 120))


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
def seeded(db: object) -> Iterator[tuple[Session, int, datetime]]:
    factory = sessionmaker(bind=db, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.US, name="REPRO TEST CORP", us_cik="9999999995")
        s.add(inst)
        s.flush()
        iid = inst.instrument_id

        candle_repo.save_revisions(s, [_row(iid, day, _price(i)) for i, day in enumerate(PERIOD)])
        s.commit()

        snapshot = snapshot_now(s)
        s.commit()

        yield s, iid, snapshot

        for table in ("candle", "instrument"):
            s.execute(text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": iid})
        s.commit()


def simulate(s: Session, iid: int, snapshot: datetime) -> bt.BacktestResult:
    return bt.run(
        STRATEGY,
        PitReader(s, data_snapshot_at=snapshot),
        instrument_id=iid,
        calendar=US,
        start=PERIOD[0],
        end=PERIOD[-1],
        starting_cash=Decimal("100000"),
        costs=CostModel(Decimal("5"), Decimal("5")),
    )


class TestDeterminism:
    def test_two_runs_of_the_same_snapshot_agree(
        self, seeded: tuple[Session, int, datetime]
    ) -> None:
        s, iid, snapshot = seeded
        first = simulate(s, iid, snapshot)
        second = simulate(s, iid, snapshot)

        assert first.equity_curve == second.equity_curve
        assert first.fills == second.fills
        assert first.trades == second.trades

    def test_the_run_actually_traded(self, seeded: tuple[Session, int, datetime]) -> None:
        """Otherwise every assertion here compares two empty runs."""
        s, iid, snapshot = seeded
        result = simulate(s, iid, snapshot)

        assert len(result.fills) >= 2
        assert result.trades
        assert result.unfilled == []


class TestBackfill:
    """A bar that arrives later must not reach a run that predates it."""

    def _backfill(self, s: Session, iid: int) -> None:
        """Restate the middle of the period, hard enough to change decisions."""
        midpoint = len(PERIOD) // 2
        candle_repo.save_revisions(
            s,
            [
                _row(iid, day, _price(i) * Decimal("3"))
                for i, day in enumerate(PERIOD)
                if midpoint <= i < midpoint + 40
            ],
        )
        s.commit()

    def test_the_run_is_unchanged_by_data_it_did_not_have(
        self, seeded: tuple[Session, int, datetime]
    ) -> None:
        s, iid, snapshot = seeded
        before = simulate(s, iid, snapshot)

        self._backfill(s, iid)

        after = simulate(s, iid, snapshot)
        assert after.equity_curve == before.equity_curve
        assert after.fills == before.fills
        assert after.trades == before.trades

    def test_the_backfill_really_would_have_changed_it(
        self, seeded: tuple[Session, int, datetime]
    ) -> None:
        """Without this, the test above passes on a backfill that never landed.

        Re-running under a snapshot taken *after* the restatement is the same
        run against a different database, and it must differ — that is what
        makes the equality above meaningful rather than vacuous.
        """
        s, iid, snapshot = seeded
        before = simulate(s, iid, snapshot)

        self._backfill(s, iid)
        wider = snapshot_now(s)
        s.commit()

        after = simulate(s, iid, wider)
        assert after.equity_curve != before.equity_curve

    def test_a_later_snapshot_sees_the_restated_prices(
        self, seeded: tuple[Session, int, datetime]
    ) -> None:
        """The bound narrows the view; it does not hide the data permanently."""
        s, iid, snapshot = seeded
        self._backfill(s, iid)
        wider = snapshot_now(s)
        s.commit()

        midpoint = len(PERIOD) // 2
        at_close = US.session_close(PERIOD[midpoint])
        old = PitReader(s, data_snapshot_at=snapshot).at(at_close)
        new = PitReader(s, data_snapshot_at=wider).at(at_close)

        assert new.bars(iid, Interval.DAY_1, limit=1)[-1].close == (
            old.bars(iid, Interval.DAY_1, limit=1)[-1].close * 3
        )


class TestExecutionHoldsOverRealData:
    def test_no_fill_lands_at_or_before_its_decision(
        self, seeded: tuple[Session, int, datetime]
    ) -> None:
        s, iid, snapshot = seeded
        result = simulate(s, iid, snapshot)

        assert all(f.execution_at > f.decision_at for f in result.fills)

    def test_every_fill_lands_on_a_session_open(
        self, seeded: tuple[Session, int, datetime]
    ) -> None:
        s, iid, snapshot = seeded
        result = simulate(s, iid, snapshot)

        for fill in result.fills:
            assert fill.execution_at == US.session_open(fill.execution_at.date())

    def test_the_opening_weeks_abstain_rather_than_judging(
        self, seeded: tuple[Session, int, datetime]
    ) -> None:
        """A 30-day average does not exist on day one, and pretending it does
        is how the start of a run quietly sets up the rest of the curve."""
        s, iid, snapshot = seeded
        result = simulate(s, iid, snapshot)

        assert result.abstained_sessions == PERIOD[: STRATEGY.long - 1]
