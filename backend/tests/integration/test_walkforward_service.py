"""Running the windows, and the claims the report is allowed to make.

Two things here are not about arithmetic.

A fitter must not see the period it will be judged on. It is handed the
training dates and nothing else, so it cannot look ahead even deliberately —
which matters, because a fitter is exactly the place where someone would.

And a report where nothing was fitted must say so. Running the same fixed
strategy on both sides of a split produces an IN/OUT gap like any other, and
that gap says nothing whatever about overfitting: no parameters were chosen,
so there was nothing to overfit. A screen showing the two side by side under
the heading "overfitting check" would be inventing evidence, so the flag that
prevents that claim travels on the report itself.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtest import strategies
from app.backtest.engine import CostModel, MarketData
from app.backtest.pit_repository import PitViolationError, snapshot_now
from app.backtest.strategies import (
    UnknownStrategyError,
    buy_and_hold,
    moving_average_cross,
)
from app.backtest.walkforward import SampleType, WalkForwardError
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.types import Interval, StrategyDefinition
from app.models import Base, Instrument
from app.repositories import candle_repo
from app.repositories.candle_repo import CandleRow
from app.services import backtest_service as svc
from app.services.backtest_service import RunRequest, StrategySpec
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

CIK = fake_cik(__name__)

US = MarketCalendar(Market.US)
HISTORY = US.sessions_between(date(2024, 1, 2), date(2024, 12, 31))

# One year and 60/30 windows rather than two years and 120/60. Same properties
# — three windows still tile, still roll, still reserve a holdout — at roughly
# a quarter of the simulation work, which these tests were spending minutes on.
TRAIN = 60
EVAL = 30
HOLDOUT = 30


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
def instrument(db: object) -> Iterator[tuple[Session, int]]:
    factory = sessionmaker(bind=db, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.US, name="WALKFORWARD CORP", us_cik=CIK)
        s.add(inst)
        s.flush()
        iid = inst.instrument_id

        candle_repo.save_revisions(
            s,
            [
                # A slow rise with a dip in the middle, so windows differ.
                _row(iid, day, Decimal(100 + (i % 97)))
                for i, day in enumerate(HISTORY)
            ],
        )
        s.commit()

        yield s, iid

        for table in ("candle", "instrument"):
            s.execute(text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": iid})
        s.commit()


def fixed(definition: StrategyDefinition | None = None) -> StrategySpec:
    return StrategySpec(definition=definition or buy_and_hold())


def fitted(fit: object) -> StrategySpec:
    return StrategySpec(fit=fit, fitter_version="test-fitter@v1")  # type: ignore[arg-type]


def request_for(iid: int) -> RunRequest:
    return RunRequest(
        instrument_id=iid,
        start=HISTORY[0],
        end=HISTORY[-1],
        starting_cash=Decimal("100000"),
        costs=CostModel(Decimal("5"), Decimal("5")),
    )


class TestTheReport:
    def test_each_window_is_measured_on_both_sides(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = svc.walk_forward(
            s, fixed(), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL
        )

        assert len(report.of(SampleType.IN_SAMPLE)) == len(report.of(SampleType.OUT_OF_SAMPLE))
        assert report.of(SampleType.OUT_OF_SAMPLE)

    def test_the_out_of_sample_span_is_reported(self, instrument: tuple[Session, int]) -> None:
        """So a reader can see how much history the figures speak for."""
        s, iid = instrument
        report = svc.walk_forward(
            s, fixed(), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL
        )
        span = report.evaluation_span

        assert span is not None
        assert span[0] > HISTORY[0]

    def test_windows_carry_their_own_caveats(self, instrument: tuple[Session, int]) -> None:
        """Abstentions and missing sessions must not be averaged away."""
        s, iid = instrument
        report = svc.walk_forward(
            s,
            fixed(moving_average_cross(short=10, long=30)),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
        )

        first_train = report.of(SampleType.IN_SAMPLE)[0]
        assert first_train.abstained > 0  # no 30-bar history at the very start
        assert all(w.without_data == 0 for w in report.windows)

    def test_one_snapshot_covers_every_window(self, instrument: tuple[Session, int]) -> None:
        """A fresh snapshot per window would widen the data under the later
        ones only, which reads as the strategy improving."""
        s, iid = instrument
        report = svc.walk_forward(
            s, fixed(), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL
        )

        assert isinstance(report.data_snapshot_at, datetime)


class TestNothingWasFitted:
    def test_a_fixed_strategy_reports_fitted_false(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = svc.walk_forward(
            s, fixed(), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL
        )

        assert report.fitted is False

    def test_supplying_a_fitter_reports_fitted_true(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument

        def fit(view: MarketData, iid: int, lo: date, hi: date) -> StrategyDefinition:
            return moving_average_cross(short=10, long=30)

        report = svc.walk_forward(
            s,
            fitted(fit),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
        )

        assert report.fitted is True


class TestTheFitterCannotSeeAhead:
    """Not "was not told about", but "cannot read".

    The first version passed the fitter a raw SQLAlchemy session and argued it
    could not look ahead because the evaluation dates were not among its
    arguments. True, and irrelevant: a query returns everything. Measured
    against Samsung, every fitter call could read all 488 bars, including the
    60 reserved as a holdout — so the isolation the generator provides was
    defeated entirely through this path.

    A fitter is the one place in a walk-forward where someone would look
    ahead, which is why it now gets the narrowest view in the system rather
    than the widest.
    """

    def test_it_receives_only_the_training_period(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        seen: list[tuple[date, date]] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            seen.append((lo, hi))
            return buy_and_hold()

        report = svc.walk_forward(
            s,
            fitted(fit),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
        )

        evaluations = report.of(SampleType.OUT_OF_SAMPLE)
        assert len(seen) == len(evaluations)
        for (_, train_end), window in zip(seen, evaluations, strict=True):
            assert train_end < window.start

    def test_the_fitted_strategy_is_the_one_evaluated(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Otherwise the fitter is decorative.

        There is no longer a second strategy to confuse it with — a spec holds
        a fixed definition or a fitter, never both — so this asserts the
        fitter's output is what the engine consults, against a fixed run that
        behaves visibly differently.
        """
        s, iid = instrument

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            return moving_average_cross(short=10, long=30)

        crossing = svc.walk_forward(
            s, fitted(fit), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL
        )
        holding = svc.walk_forward(
            s, fixed(), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL
        )

        # The crossover has no 30-bar history at the very start and abstains;
        # buy-and-hold judges from the first session.
        assert crossing.of(SampleType.IN_SAMPLE)[0].abstained == 29
        assert holding.of(SampleType.IN_SAMPLE)[0].abstained == 0
        assert all(w.chosen.kind == "moving_average_cross" for w in crossing.windows)
        assert all(w.chosen.kind == "buy_and_hold" for w in holding.windows)

    def test_it_cannot_read_a_bar_from_after_the_training_period(
        self, instrument: tuple[Session, int]
    ) -> None:
        """A distinctive bar planted past every training window."""
        s, iid = instrument
        candle_repo.save_revisions(s, [_row(iid, HISTORY[-1], Decimal("999"))])
        s.commit()

        latest: list[Decimal] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            latest.append(bars[-1].close)
            assert bars[-1].ts.date() <= hi
            return buy_and_hold()

        svc.walk_forward(
            s,
            fitted(fit),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
            holdout_sessions=HOLDOUT,
        )

        assert latest
        assert Decimal("999") not in latest

    def test_positioning_past_the_training_end_raises(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Reaching for it is refused, not quietly answered with less."""
        s, iid = instrument
        refused: list[bool] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            try:
                view.at(US.session_close(HISTORY[-1]))
                refused.append(False)
            except PitViolationError:
                refused.append(True)
            return buy_and_hold()

        svc.walk_forward(
            s,
            fitted(fit),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
            holdout_sessions=HOLDOUT,
        )

        assert refused and all(refused)

    def test_it_cannot_widen_its_own_view_by_rebounding(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Bounds only tighten."""
        s, iid = instrument
        results: list[bool] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            wider = view.windowed(  # type: ignore[attr-defined]
                not_before=US.session_open(HISTORY[0]),
                not_after=US.session_close(HISTORY[-1]),
            )
            try:
                wider.at(US.session_close(HISTORY[-1]))
                results.append(False)
            except PitViolationError:
                results.append(True)
            return buy_and_hold()

        svc.walk_forward(s, fitted(fit), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL)

        assert results and all(results)

    def test_a_later_backfill_is_invisible_to_the_fitter(
        self, instrument: tuple[Session, int]
    ) -> None:
        """The snapshot binds here too, not only inside the simulation."""
        s, iid = instrument
        snapshot = snapshot_now(s)
        s.commit()

        early = HISTORY[10]
        candle_repo.save_revisions(s, [_row(iid, early, Decimal("777"))])
        s.commit()

        seen: list[Decimal] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            seen.extend(b.close for b in bars if b.ts.date() == early)
            return buy_and_hold()

        svc.walk_forward(
            s,
            fitted(fit),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
            data_snapshot_at=snapshot,
        )

        assert seen
        assert Decimal("777") not in seen


class TestRollingTrainingActuallyRolls:
    """The floor, which is what separates rolling from anchored.

    The ceiling stops the fitter reading the future. Nothing stopped it
    reading the past, so `bars` looked back as far as the database went.
    Measured on Samsung with a 120-session rolling split, the fitter saw:

        window 0   120 bars   (rolling)
        window 1   180 bars
        window 2   240 bars
        window 3   299 bars
        window 4   359 bars

    Only the first window was rolling. For the rest, `rolling` and `anchored`
    differed in the dates printed beside the result and in nothing else.
    """

    def test_the_fitter_cannot_see_before_its_training_window(
        self, instrument: tuple[Session, int]
    ) -> None:
        """A distinctive bar at the very start of history, which only window 0
        may see."""
        s, iid = instrument
        candle_repo.save_revisions(s, [_row(iid, HISTORY[0], Decimal("555"))])
        s.commit()

        earliest: list[date] = []
        saw_555: list[bool] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            earliest.append(bars[0].ts.date())
            saw_555.append(any(b.close == Decimal("555") for b in bars))
            return buy_and_hold()

        report = svc.walk_forward(
            s,
            fitted(fit),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
        )

        assert len(earliest) > 1
        # Only the first window starts at the first session, so only it sees it.
        assert saw_555[0] is True
        assert not any(saw_555[1:])
        assert len(report.windows) == 2 * len(earliest)

    def test_each_window_starts_at_its_own_training_start(
        self, instrument: tuple[Session, int]
    ) -> None:
        s, iid = instrument
        starts: list[tuple[date, date]] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            starts.append((lo, bars[0].ts.date()))
            return buy_and_hold()

        svc.walk_forward(s, fitted(fit), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL)

        assert starts
        for train_start, first_bar in starts:
            assert first_bar == train_start

    def test_the_training_sample_is_the_length_that_was_asked_for(
        self, instrument: tuple[Session, int]
    ) -> None:
        """The count that grew with every window before the floor existed."""
        s, iid = instrument
        counts: list[int] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            counts.append(len(bars))
            return buy_and_hold()

        svc.walk_forward(s, fitted(fit), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL)

        assert counts == [TRAIN] * len(counts)

    def test_an_anchored_split_does_start_at_the_beginning(
        self, instrument: tuple[Session, int]
    ) -> None:
        """The floor must follow the split, not override it."""
        s, iid = instrument
        starts: list[date] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            starts.append(bars[0].ts.date())
            return buy_and_hold()

        svc.walk_forward(
            s,
            fitted(fit),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
            anchored=True,
        )

        assert starts
        assert set(starts) == {HISTORY[0]}

    def test_reaching_below_the_floor_raises(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        refused: list[bool] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            try:
                view.at(US.session_close(HISTORY[0]))
                refused.append(False)
            except PitViolationError:
                refused.append(True)
            return buy_and_hold()

        svc.walk_forward(s, fitted(fit), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL)

        # Window 0's floor is the first session, so only later windows refuse.
        assert refused[0] is False
        assert all(refused[1:])


class TestFoldsAreIndependentRuns:
    """The dates tile; the portfolios do not continue across them."""

    def test_each_window_is_measured_on_its_own(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = svc.walk_forward(
            s, fixed(), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL
        )

        out = report.of(SampleType.OUT_OF_SAMPLE)
        assert len(out) > 1
        # Buy-and-hold never sells, so a window inheriting a position would
        # have no cash and record no fills of its own.
        assert all(w.performance is not None for w in out)
        assert all(w.sessions == EVAL for w in out)


class TestWalkForwardNeverScoresTheHoldout:
    """Scoring it is a separate call, on purpose.

    A holdout reported on every iteration gets fitted by eye — someone adjusts
    the window lengths, sees the number move, adjusts again. That is harder to
    notice than fitting it in code and no less real, so `walk_forward` cannot
    produce a HOLDOUT result at all.
    """

    def test_it_produces_no_holdout_result(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = svc.walk_forward(
            s,
            fixed(),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
            holdout_sessions=HOLDOUT,
        )

        assert report.of(SampleType.HOLDOUT) == ()

    def test_no_window_touches_the_reserved_tail(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = svc.walk_forward(
            s,
            fixed(),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
            holdout_sessions=HOLDOUT,
        )

        assert report.holdout_start is not None
        assert all(w.end < report.holdout_start for w in report.windows)

    def test_a_split_that_cannot_be_made_raises(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        with pytest.raises(WalkForwardError, match="sessions"):
            svc.walk_forward(
                s,
                fixed(),
                request_for(iid),
                train_sessions=10_000,
                eval_sessions=EVAL,
            )


class TestTheFinalHoldoutEvaluation:
    """The one measurement nothing was allowed to iterate against.

    Run after every choice has been made, on a period no window, no fitter and
    no earlier run could read. It is the only figure in the system that was
    not available while the rule was being chosen, which is the whole of its
    value — and it survives only because scoring it is a deliberate act rather
    than something `walk_forward` returns for free.

    It also takes the report and nothing else. An earlier version accepted the
    strategy, the request and the fitter again, so a caller could conclude the
    Apple experiment with a Samsung run, or score a fitted run without its
    fitter. Both were accepted, and both moved the number:

        AAPL, fitted, 10,000 cash, 5bp      honest        -1.72%
        scored against Samsung instead                   -16.63%
        scored with fit=None instead                     +21.91%
    """

    def _report(
        self, s: Session, iid: int, spec: StrategySpec | None = None, **kwargs: object
    ) -> svc.WalkForwardReport:
        return svc.walk_forward(
            s,
            spec if spec is not None else fixed(),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
            holdout_sessions=HOLDOUT,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_it_covers_exactly_the_reserved_tail(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = self._report(s, iid)

        final = svc.evaluate_holdout(s, report)

        assert final.sample_type is SampleType.HOLDOUT
        assert (final.start, final.end) == (report.holdout_start, report.holdout_end)
        assert final.sessions == HOLDOUT

    def test_a_run_with_no_holdout_refuses(self, instrument: tuple[Session, int]) -> None:
        """Reserving it afterwards is not reserving it."""
        s, iid = instrument
        report = svc.walk_forward(
            s, fixed(), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL
        )

        with pytest.raises(svc.HoldoutError, match="reserved no holdout"):
            svc.evaluate_holdout(s, report)

    def test_it_reuses_the_run_snapshot(self, instrument: tuple[Session, int]) -> None:
        """A holdout scored against a different snapshot concludes a different run."""
        s, iid = instrument
        report = self._report(s, iid)

        # A restatement landing after the walk-forward must not reach it.
        candle_repo.save_revisions(s, [_row(iid, HISTORY[-1], Decimal("4242"))])
        s.commit()

        assert svc.evaluate_holdout(s, report) == svc.evaluate_holdout(s, report)

    def test_the_request_cannot_be_swapped(self) -> None:
        """The instrument, the cash and the costs all come from the report.

        Before this, a Samsung request was accepted as the conclusion of an
        Apple experiment.
        """
        params = list(inspect.signature(svc.evaluate_holdout).parameters)
        assert params == ["session", "report"]

    def test_a_fitted_run_is_concluded_with_its_fitter(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Dropping it moved the Apple holdout 23 points, in the flattering
        direction. The fitter travels on the spec now, so it cannot be left
        out or swapped in."""
        s, iid = instrument
        calls: list[int] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            calls.append(1)
            return buy_and_hold()

        report = self._report(s, iid, spec=fitted(fit))
        during_walk_forward = len(calls)

        svc.evaluate_holdout(s, report)

        assert report.fitted is True
        assert len(calls) == during_walk_forward + 1

    def test_the_final_fitter_cannot_see_the_holdout(self, instrument: tuple[Session, int]) -> None:
        """It refits on everything up to the session before it opens."""
        s, iid = instrument
        seen: list[tuple[date, date, date]] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            seen.append((lo, hi, bars[-1].ts.date()))
            return buy_and_hold()

        report = self._report(s, iid, spec=fitted(fit))
        assert report.holdout_start is not None
        seen.clear()  # only the final fit is of interest

        svc.evaluate_holdout(s, report)

        assert len(seen) == 1
        train_start, train_end, latest = seen[0]
        assert train_end < report.holdout_start
        assert latest <= train_end
        assert train_start < train_end

    def test_reaching_into_the_holdout_raises(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        holdout_end: list[date] = []
        refused: list[bool] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            if holdout_end:
                try:
                    view.at(US.session_close(holdout_end[0]))
                    refused.append(False)
                except PitViolationError:
                    refused.append(True)
            return buy_and_hold()

        report = self._report(s, iid, spec=fitted(fit))
        assert report.holdout_end is not None
        holdout_end.append(report.holdout_end)

        svc.evaluate_holdout(s, report)

        assert refused == [True]

    def test_the_final_training_window_rolls_when_the_split_rolled(
        self, instrument: tuple[Session, int]
    ) -> None:
        s, iid = instrument
        lengths: list[int] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            lengths.append(len(bars))
            return buy_and_hold()

        report = self._report(s, iid, spec=fitted(fit))
        lengths.clear()

        svc.evaluate_holdout(s, report)

        assert lengths == [TRAIN]

    def test_it_is_anchored_when_the_split_was_anchored(
        self, instrument: tuple[Session, int]
    ) -> None:
        """The final fit must follow the run it concludes, not its own default."""
        s, iid = instrument
        starts: list[date] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            starts.append(bars[0].ts.date())
            return buy_and_hold()

        report = self._report(s, iid, spec=fitted(fit), anchored=True)
        starts.clear()

        svc.evaluate_holdout(s, report)

        assert starts == [HISTORY[0]]


class TestTheSpecIsTheExperiment:
    """A run is a fixed rule or a fitted one, throughout — and it is rebuildable.

    An earlier spec carried a version, free-form params and a live strategy
    object as three independent fields, so all three could disagree:

        StrategySpec(
            version="ma-cross@v1",
            strategy=MovingAverageCross(short=10, long=30),
            params={"short": 20, "long": 60},
        )

    ran 10/30 and would have stored 20/60. Two specs could also share a
    version and behave differently. Neither broke a backtest; both broke the
    reproducibility a stored run exists to provide.
    """

    def test_both_a_definition_and_a_fitter_is_refused(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            StrategySpec(definition=buy_and_hold(), fit=lambda *a: buy_and_hold())

    def test_neither_is_refused(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            StrategySpec()

    def test_a_fitted_run_needs_a_fitter_version(self) -> None:
        """The fitter chose the parameters, so it is the reproducible thing."""
        with pytest.raises(ValueError, match="fitter_version"):
            StrategySpec(fit=lambda *a: buy_and_hold())

    def test_a_fixed_version_comes_from_the_definition(self) -> None:
        """Not written beside it, so the two cannot disagree."""
        spec = StrategySpec(definition=moving_average_cross(short=10, long=30))

        assert spec.version == spec.definition.version  # type: ignore[union-attr]
        assert spec.version == "ma-10-30@v1"

    def test_params_are_the_constructors_arguments(self) -> None:
        """So what a row says and what runs cannot drift apart silently."""
        definition = moving_average_cross(short=10, long=30)
        built = strategies.build(definition)

        assert definition.params == {"short": 10, "long": 30}
        assert (built.short, built.long) == (10, 30)  # type: ignore[union-attr]

    def test_a_definition_that_cannot_be_built_is_refused(self) -> None:
        with pytest.raises(UnknownStrategyError, match="no strategy kind"):
            strategies.build(StrategyDefinition(kind="does_not_exist", version="v1"))

    def test_a_misspelled_parameter_fails_rather_than_defaulting(self) -> None:
        """Falling back to a default would run something the row does not say."""
        with pytest.raises(UnknownStrategyError, match="cannot build"):
            strategies.build(
                StrategyDefinition(
                    kind="moving_average_cross",
                    version="v1",
                    params={"shrot": 10, "long": 30},
                )
            )

    def test_the_report_carries_the_whole_experiment(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = svc.walk_forward(
            s,
            fixed(moving_average_cross(short=10, long=30)),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
        )

        assert report.spec.version == "ma-10-30@v1"
        assert report.request == request_for(iid)
        assert report.eval_sessions == EVAL

    def test_every_window_records_what_actually_ran(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = svc.walk_forward(
            s,
            fixed(moving_average_cross(short=10, long=30)),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
        )

        assert all(w.chosen.params == {"short": 10, "long": 30} for w in report.windows)

    def test_a_fitter_records_each_folds_own_choice(self, instrument: tuple[Session, int]) -> None:
        """A run storing only the fitter's name cannot say why window 3
        behaved as it did."""
        s, iid = instrument
        lengths = iter([10, 15, 20, 25, 30, 35, 40, 45])

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> StrategyDefinition:
            return moving_average_cross(short=next(lengths), long=60)

        report = svc.walk_forward(
            s, fitted(fit), request_for(iid), train_sessions=TRAIN, eval_sessions=EVAL
        )

        chosen = [w.chosen.params["short"] for w in report.of(SampleType.OUT_OF_SAMPLE)]
        assert len(set(chosen)) == len(chosen)
        assert report.spec.version == "test-fitter@v1"
