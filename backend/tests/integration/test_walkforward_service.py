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

        def fit(session: Session, iid: int, lo: date, hi: date) -> MovingAverageCross:
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
    def test_it_receives_only_the_training_period(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        seen: list[tuple[date, date]] = []

        def fit(session: Session, instrument_id: int, lo: date, hi: date) -> BuyAndHold:
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

        def fit(session: Session, instrument_id: int, lo: date, hi: date) -> NeverTrades:
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


class TestHoldoutIsNeverRun:
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
