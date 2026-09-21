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

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtest.engine import CostModel, MarketData, Signal
from app.backtest.pit_repository import PitViolationError, snapshot_now
from app.backtest.strategies import BuyAndHold, MovingAverageCross
from app.backtest.walkforward import SampleType, WalkForwardError
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.types import Interval
from app.models import Base, Instrument
from app.repositories import candle_repo
from app.repositories.candle_repo import CandleRow
from app.services import backtest_service as svc
from app.services.backtest_service import RunRequest

pytestmark = pytest.mark.integration

US = MarketCalendar(Market.US)
HISTORY = US.sessions_between(date(2024, 1, 2), date(2025, 12, 31))


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
        inst = Instrument(market=Market.US, name="WALKFORWARD CORP", us_cik="9999999993")
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
            s, BuyAndHold(), request_for(iid), train_sessions=120, eval_sessions=60
        )

        assert len(report.of(SampleType.IN_SAMPLE)) == len(report.of(SampleType.OUT_OF_SAMPLE))
        assert report.of(SampleType.OUT_OF_SAMPLE)

    def test_the_out_of_sample_span_is_reported(self, instrument: tuple[Session, int]) -> None:
        """So a reader can see how much history the figures speak for."""
        s, iid = instrument
        report = svc.walk_forward(
            s, BuyAndHold(), request_for(iid), train_sessions=120, eval_sessions=60
        )
        span = report.evaluation_span

        assert span is not None
        assert span[0] > HISTORY[0]

    def test_windows_carry_their_own_caveats(self, instrument: tuple[Session, int]) -> None:
        """Abstentions and missing sessions must not be averaged away."""
        s, iid = instrument
        report = svc.walk_forward(
            s,
            MovingAverageCross(short=10, long=30),
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
        )

        first_train = report.of(SampleType.IN_SAMPLE)[0]
        assert first_train.abstained > 0  # no 30-bar history at the very start
        assert all(w.without_data == 0 for w in report.windows)

    def test_one_snapshot_covers_every_window(self, instrument: tuple[Session, int]) -> None:
        """A fresh snapshot per window would widen the data under the later
        ones only, which reads as the strategy improving."""
        s, iid = instrument
        report = svc.walk_forward(
            s, BuyAndHold(), request_for(iid), train_sessions=120, eval_sessions=60
        )

        assert isinstance(report.data_snapshot_at, datetime)


class TestNothingWasFitted:
    def test_a_fixed_strategy_reports_fitted_false(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = svc.walk_forward(
            s, BuyAndHold(), request_for(iid), train_sessions=120, eval_sessions=60
        )

        assert report.fitted is False

    def test_supplying_a_fitter_reports_fitted_true(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument

        def fit(view: MarketData, iid: int, lo: date, hi: date) -> MovingAverageCross:
            return MovingAverageCross(short=10, long=30)

        report = svc.walk_forward(
            s,
            BuyAndHold(),
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
            fit=fit,
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

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            seen.append((lo, hi))
            return BuyAndHold()

        report = svc.walk_forward(
            s,
            BuyAndHold(),
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
            fit=fit,
        )

        evaluations = report.of(SampleType.OUT_OF_SAMPLE)
        assert len(seen) == len(evaluations)
        for (_, train_end), window in zip(seen, evaluations, strict=True):
            assert train_end < window.start

    def test_the_fitted_strategy_is_the_one_evaluated(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Otherwise the fitter is decorative."""
        s, iid = instrument

        class NeverTrades:
            def evaluate(self, data: MarketData, instrument_id: int) -> Signal:
                return Signal.HOLD

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> NeverTrades:
            return NeverTrades()

        report = svc.walk_forward(
            s,
            BuyAndHold(),  # would trade on every window if it were used
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
            fit=fit,
        )

        assert all(w.trades == 0 for w in report.windows)

    def test_it_cannot_read_a_bar_from_after_the_training_period(
        self, instrument: tuple[Session, int]
    ) -> None:
        """A distinctive bar planted past every training window."""
        s, iid = instrument
        candle_repo.save_revisions(s, [_row(iid, HISTORY[-1], Decimal("999"))])
        s.commit()

        latest: list[Decimal] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            latest.append(bars[-1].close)
            assert bars[-1].ts.date() <= hi
            return BuyAndHold()

        svc.walk_forward(
            s,
            BuyAndHold(),
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
            holdout_sessions=60,
            fit=fit,
        )

        assert latest
        assert Decimal("999") not in latest

    def test_positioning_past_the_training_end_raises(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Reaching for it is refused, not quietly answered with less."""
        s, iid = instrument
        refused: list[bool] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            try:
                view.at(US.session_close(HISTORY[-1]))
                refused.append(False)
            except PitViolationError:
                refused.append(True)
            return BuyAndHold()

        svc.walk_forward(
            s,
            BuyAndHold(),
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
            holdout_sessions=60,
            fit=fit,
        )

        assert refused and all(refused)

    def test_it_cannot_widen_its_own_view_by_rebounding(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Ceilings only tighten."""
        s, iid = instrument
        results: list[bool] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            wider = view.windowed(  # type: ignore[attr-defined]
                not_before=US.session_open(HISTORY[0]),
                not_after=US.session_close(HISTORY[-1]),
            )
            try:
                wider.at(US.session_close(HISTORY[-1]))
                results.append(False)
            except PitViolationError:
                results.append(True)
            return BuyAndHold()

        svc.walk_forward(
            s, BuyAndHold(), request_for(iid), train_sessions=120, eval_sessions=60, fit=fit
        )

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

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            seen.extend(b.close for b in bars if b.ts.date() == early)
            return BuyAndHold()

        svc.walk_forward(
            s,
            BuyAndHold(),
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
            fit=fit,
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

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            earliest.append(bars[0].ts.date())
            saw_555.append(any(b.close == Decimal("555") for b in bars))
            return BuyAndHold()

        report = svc.walk_forward(
            s,
            BuyAndHold(),
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
            fit=fit,
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

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            starts.append((lo, bars[0].ts.date()))
            return BuyAndHold()

        svc.walk_forward(
            s, BuyAndHold(), request_for(iid), train_sessions=120, eval_sessions=60, fit=fit
        )

        assert starts
        for train_start, first_bar in starts:
            assert first_bar == train_start

    def test_the_training_sample_is_the_length_that_was_asked_for(
        self, instrument: tuple[Session, int]
    ) -> None:
        """The count that grew with every window before the floor existed."""
        s, iid = instrument
        counts: list[int] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            counts.append(len(bars))
            return BuyAndHold()

        svc.walk_forward(
            s, BuyAndHold(), request_for(iid), train_sessions=120, eval_sessions=60, fit=fit
        )

        assert counts == [120] * len(counts)

    def test_an_anchored_split_does_start_at_the_beginning(
        self, instrument: tuple[Session, int]
    ) -> None:
        """The floor must follow the split, not override it."""
        s, iid = instrument
        starts: list[date] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            starts.append(bars[0].ts.date())
            return BuyAndHold()

        svc.walk_forward(
            s,
            BuyAndHold(),
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
            anchored=True,
            fit=fit,
        )

        assert starts
        assert set(starts) == {HISTORY[0]}

    def test_reaching_below_the_floor_raises(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        refused: list[bool] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            try:
                view.at(US.session_close(HISTORY[0]))
                refused.append(False)
            except PitViolationError:
                refused.append(True)
            return BuyAndHold()

        svc.walk_forward(
            s, BuyAndHold(), request_for(iid), train_sessions=120, eval_sessions=60, fit=fit
        )

        # Window 0's floor is the first session, so only later windows refuse.
        assert refused[0] is False
        assert all(refused[1:])


class TestFoldsAreIndependentRuns:
    """The dates tile; the portfolios do not continue across them."""

    def test_each_window_is_measured_on_its_own(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = svc.walk_forward(
            s, BuyAndHold(), request_for(iid), train_sessions=120, eval_sessions=60
        )

        out = report.of(SampleType.OUT_OF_SAMPLE)
        assert len(out) > 1
        # Buy-and-hold never sells, so a window inheriting a position would
        # have no cash and record no fills of its own.
        assert all(w.performance is not None for w in out)
        assert all(w.sessions == 60 for w in out)


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
            BuyAndHold(),
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
            holdout_sessions=60,
        )

        assert report.of(SampleType.HOLDOUT) == ()

    def test_no_window_touches_the_reserved_tail(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = svc.walk_forward(
            s,
            BuyAndHold(),
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
            holdout_sessions=60,
        )

        assert report.holdout_start is not None
        assert all(w.end < report.holdout_start for w in report.windows)

    def test_a_split_that_cannot_be_made_raises(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        with pytest.raises(WalkForwardError, match="sessions"):
            svc.walk_forward(
                s,
                BuyAndHold(),
                request_for(iid),
                train_sessions=10_000,
                eval_sessions=60,
            )


class TestTheFinalHoldoutEvaluation:
    """The one measurement nothing was allowed to iterate against.

    Run after every choice has been made, on a period no window, no fitter and
    no earlier run could read. It is the only figure in the system that was
    not available while the rule was being chosen, which is the whole of its
    value — and it survives only because scoring it is a deliberate act rather
    than something `walk_forward` returns for free.
    """

    def _report(self, s: Session, iid: int, **kwargs: object) -> object:
        return svc.walk_forward(
            s,
            BuyAndHold(),
            request_for(iid),
            train_sessions=120,
            eval_sessions=60,
            holdout_sessions=60,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_it_covers_exactly_the_reserved_tail(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = self._report(s, iid)

        final = svc.evaluate_holdout(s, BuyAndHold(), request_for(iid), report)  # type: ignore[arg-type]

        assert final.sample_type is SampleType.HOLDOUT
        assert (final.start, final.end) == (report.holdout_start, report.holdout_end)  # type: ignore[attr-defined]
        assert final.sessions == 60

    def test_it_is_a_single_result(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = self._report(s, iid)

        final = svc.evaluate_holdout(s, BuyAndHold(), request_for(iid), report)  # type: ignore[arg-type]

        assert isinstance(final, svc.WindowResult)

    def test_a_run_with_no_holdout_refuses(self, instrument: tuple[Session, int]) -> None:
        """Reserving it afterwards is not reserving it."""
        s, iid = instrument
        report = svc.walk_forward(
            s, BuyAndHold(), request_for(iid), train_sessions=120, eval_sessions=60
        )

        with pytest.raises(svc.HoldoutError, match="reserved no holdout"):
            svc.evaluate_holdout(s, BuyAndHold(), request_for(iid), report)

    def test_it_reuses_the_run_snapshot(self, instrument: tuple[Session, int]) -> None:
        """A holdout scored against a different snapshot concludes a different run."""
        s, iid = instrument
        report = self._report(s, iid)

        # A restatement landing after the walk-forward must not reach it.
        candle_repo.save_revisions(s, [_row(iid, HISTORY[-1], Decimal("4242"))])
        s.commit()

        final = svc.evaluate_holdout(s, BuyAndHold(), request_for(iid), report)  # type: ignore[arg-type]
        again = svc.evaluate_holdout(s, BuyAndHold(), request_for(iid), report)  # type: ignore[arg-type]

        assert final == again

    def test_the_final_fitter_cannot_see_the_holdout(self, instrument: tuple[Session, int]) -> None:
        """It refits on everything up to the session before it opens."""
        s, iid = instrument
        report = self._report(s, iid)
        assert report.holdout_start is not None  # type: ignore[attr-defined]
        seen: list[tuple[date, date, date]] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            seen.append((lo, hi, bars[-1].ts.date()))
            return BuyAndHold()

        svc.evaluate_holdout(s, BuyAndHold(), request_for(iid), report, fit=fit)  # type: ignore[arg-type]

        assert len(seen) == 1
        train_start, train_end, latest = seen[0]
        assert train_end < report.holdout_start  # type: ignore[attr-defined]
        assert latest <= train_end
        assert train_start < train_end

    def test_reaching_into_the_holdout_raises(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report = self._report(s, iid)
        assert report.holdout_end is not None  # type: ignore[attr-defined]
        refused: list[bool] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            try:
                view.at(US.session_close(report.holdout_end))  # type: ignore[attr-defined]
                refused.append(False)
            except PitViolationError:
                refused.append(True)
            return BuyAndHold()

        svc.evaluate_holdout(s, BuyAndHold(), request_for(iid), report, fit=fit)  # type: ignore[arg-type]

        assert refused == [True]

    def test_the_final_training_window_rolls_when_the_split_rolled(
        self, instrument: tuple[Session, int]
    ) -> None:
        s, iid = instrument
        report = self._report(s, iid)
        lengths: list[int] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            lengths.append(len(bars))
            return BuyAndHold()

        svc.evaluate_holdout(s, BuyAndHold(), request_for(iid), report, fit=fit)  # type: ignore[arg-type]

        assert lengths == [120]

    def test_it_is_anchored_when_the_split_was_anchored(
        self, instrument: tuple[Session, int]
    ) -> None:
        """The final fit must follow the run it concludes, not its own default."""
        s, iid = instrument
        report = self._report(s, iid, anchored=True)
        starts: list[date] = []

        def fit(view: MarketData, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
            bars = view.at(US.session_close(hi)).bars(instrument_id, Interval.DAY_1, limit=10000)
            starts.append(bars[0].ts.date())
            return BuyAndHold()

        svc.evaluate_holdout(s, BuyAndHold(), request_for(iid), report, fit=fit)  # type: ignore[arg-type]

        assert starts == [HISTORY[0]]
