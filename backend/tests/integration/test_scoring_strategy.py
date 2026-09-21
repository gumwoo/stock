"""The system's own rule, run through the backtest.

Every strategy before this was a harness. A moving-average cross exercises the
point-in-time filters, the execution clock and the persistence layer, and says
nothing about whether this system's judgement is any good. This is the rule
the dashboard shows, running under the same machinery.

Two properties matter more than any number it produces.

The policy is shared, not copied. A backtest that redeclared the weights or
the thresholds would be measuring a different strategy from the one the system
runs, and the difference would be invisible — both produce scores, both look
plausible, nothing compares them.

And the rule must actually vary. A strategy that returns one signal forever
would pass every mechanical test here while measuring nothing, so the spread
of decisions is asserted directly.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtest import engine as bt
from app.backtest import strategies
from app.backtest.engine import CostModel, Signal
from app.backtest.pit_repository import snapshot_now
from app.backtest.scoring_strategy import TechnicalFundamental
from app.backtest.strategies import technical_fundamental
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.types import Interval
from app.models import Base, Instrument
from app.repositories import candle_repo
from app.repositories.candle_repo import CandleRow
from app.scoring import policy
from app.services import backtest_service as svc
from app.services import scoring_service
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

CIK = fake_cik(__name__)
US = MarketCalendar(Market.US)
HISTORY = US.sessions_between(date(2023, 1, 3), date(2024, 12, 31))


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
    """A long rise, a fall, and a rise — enough for the score to move."""
    if index < 200:
        return Decimal(100 + index)
    if index < 350:
        return Decimal(300 - (index - 200))
    return Decimal(150 + (index - 350))


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
        inst = Instrument(market=Market.US, name="SCORING TEST CORP", us_cik=CIK)
        s.add(inst)
        s.flush()

        candle_repo.save_revisions(
            s,
            [_row(inst.instrument_id, day, _price(i)) for i, day in enumerate(HISTORY)],
        )
        s.commit()

        yield s, inst

        s.execute(
            text("DELETE FROM backtest_run WHERE instrument_id = :i"),
            {"i": inst.instrument_id},
        )
        for table in ("candle", "instrument"):
            s.execute(
                text(f"DELETE FROM {table} WHERE instrument_id = :i"),
                {"i": inst.instrument_id},
            )
        s.commit()


def decisions(s: Session, inst: Instrument) -> tuple[Counter[str], bt.BacktestResult]:
    seen: Counter[str] = Counter()
    rule = TechnicalFundamental(currency=svc.CURRENCY[inst.market])

    class Watching:
        def evaluate(self, data: object, instrument_id: int) -> Signal:
            verdict = rule.evaluate(data, instrument_id)  # type: ignore[arg-type]
            seen[str(verdict)] += 1
            return verdict

    result = bt.run(
        Watching(),
        svc.reader_for(s, inst, snapshot_now(s)),
        instrument_id=inst.instrument_id,
        calendar=US,
        start=HISTORY[0],
        end=HISTORY[-1],
        starting_cash=Decimal("100000"),
        costs=CostModel(Decimal("5"), Decimal("5")),
    )
    return seen, result


class TestItIsTheSameRuleTheSystemRuns:
    """A copied policy would measure a different strategy, invisibly."""

    def test_the_weights_come_from_the_shared_policy(self) -> None:
        import app.backtest.scoring_strategy as strategy_module

        assert strategy_module.WEIGHTS is policy.WEIGHTS

    def test_the_live_scorer_reads_the_same_ones(self) -> None:
        assert scoring_service.WEIGHTS is policy.WEIGHTS
        assert scoring_service.REQUIRED is policy.REQUIRED

    def test_the_stored_version_tracks_the_policy(self) -> None:
        """A change to the weights is a change to this strategy, even when
        none of its own parameters moved."""
        definition = technical_fundamental()

        assert policy.STRATEGY_VERSION in definition.version

    def test_the_definition_rebuilds_the_rule(self) -> None:
        built = strategies.build(technical_fundamental(buy_interest=65, caution=30))

        assert isinstance(built, TechnicalFundamental)
        assert (built.buy_interest, built.caution) == (65, 30)


class TestTheRuleActuallyVaries:
    """A strategy returning one signal forever would pass every mechanical
    test here while measuring nothing."""

    def test_it_produces_more_than_one_decision(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        s, inst = instrument
        seen, _ = decisions(s, inst)

        assert len(seen) > 1, f"the rule never changed its mind: {dict(seen)}"

    def test_technical_alone_cannot_reach_the_buy_threshold(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        """A property of the current policy, found by running it.

        This instrument has no filings, so the fundamental factor stands down
        and only technical participates at weight 0.6 — which scales the buy
        threshold to 42, needing a technical score of 70. Measured directly on
        constructed series, the engine tops out well short of that:

            steady ramp, flat volume            54.2
            steady ramp, rising volume          59.7
            compounding trend, rising volume    62.5
            trend with oscillation              55.1

        So an instrument without fundamentals can hold or exit but never
        enter. That is not a bug here — the thresholds are deliberate and
        BUY_INTEREST is meant to be rare — but it means the rule is, in
        practice, gated on having financials at all. Pinned as a test so the
        day someone moves a weight or a threshold, this either still holds or
        fails loudly.
        """
        s, inst = instrument
        seen, result = decisions(s, inst)

        assert seen["ENTER"] == 0, f"the policy changed: {dict(seen)}"
        assert result.fills == []

    def test_lowering_the_threshold_does_let_it_trade(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        """Which shows the ceiling above is the threshold, not the plumbing."""
        s, inst = instrument
        eager = TechnicalFundamental(buy_interest=50.0, caution=20.0, currency="USD")

        result = bt.run(
            eager,
            svc.reader_for(s, inst, snapshot_now(s)),
            instrument_id=inst.instrument_id,
            calendar=US,
            start=HISTORY[0],
            end=HISTORY[-1],
            starting_cash=Decimal("100000"),
            costs=CostModel(Decimal("5"), Decimal("5")),
        )

        assert result.fills, "the rule never reached the market even at 50"

    def test_it_abstains_before_it_has_history(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        """The opening sessions have no indicator to judge from, and a HOLD
        there would be a judgement made from an average that does not exist."""
        s, inst = instrument
        rule = TechnicalFundamental(currency=svc.CURRENCY[inst.market])
        reader = svc.reader_for(s, inst, snapshot_now(s))

        early = reader.at(US.session_close(HISTORY[3]))

        assert rule.evaluate(early, inst.instrument_id) is Signal.ABSTAIN


class TestFundamentalsComeThroughTheSameDoor:
    def test_the_snapshot_is_bound_to_the_simulated_instant(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        """A fundamental lookup is under the same point-in-time rules as a
        price lookup, or the rule could read filings from its own future."""
        s, inst = instrument
        reader = svc.reader_for(s, inst, snapshot_now(s))
        moment = US.session_close(HISTORY[100])

        snapshot = reader.at(moment).fundamentals(inst.instrument_id, price=100.0, currency="USD")

        assert snapshot.asof == moment

    def test_an_instrument_with_no_filings_still_scores(
        self, instrument: tuple[Session, Instrument]
    ) -> None:
        """Fundamentals are not required, so their absence stands the factor
        down rather than silencing the whole rule."""
        s, inst = instrument
        seen, _ = decisions(s, inst)

        assert sum(seen.values()) == len(HISTORY)
        assert seen["ABSTAIN"] < len(HISTORY)


class TestItStoresAndReproduces:
    def test_a_walk_forward_runs_end_to_end(self, instrument: tuple[Session, Instrument]) -> None:
        s, inst = instrument
        report = svc.walk_forward(
            s,
            svc.StrategySpec(definition=technical_fundamental(currency="USD")),
            svc.RunRequest(
                instrument_id=inst.instrument_id,
                start=HISTORY[0],
                end=HISTORY[-1],
                starting_cash=Decimal("100000"),
                costs=CostModel(Decimal("5"), Decimal("5")),
            ),
            train_sessions=120,
            eval_sessions=60,
            holdout_sessions=60,
        )
        run = svc.persist(s, report)
        s.commit()

        assert run.strategy_kind == "technical_fundamental"
        assert run.strategy_params["buy_interest"] == 70.0

    def test_the_stored_run_reproduces(self, instrument: tuple[Session, Instrument]) -> None:
        from app.services import reproduce_service as rs

        s, inst = instrument
        report = svc.walk_forward(
            s,
            svc.StrategySpec(definition=technical_fundamental(currency="USD")),
            svc.RunRequest(
                instrument_id=inst.instrument_id,
                start=HISTORY[0],
                end=HISTORY[-1],
                starting_cash=Decimal("100000"),
                costs=CostModel(Decimal("5"), Decimal("5")),
            ),
            train_sessions=120,
            eval_sessions=60,
        )
        run = svc.persist(s, report)
        s.commit()

        assert rs.reproduce(s, run.id).reproduced
