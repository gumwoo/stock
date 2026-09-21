"""What a stored run has to be able to prove.

A number without its provenance is an anecdote. These tests pin the three
axes that decide a result — the strategy, the code, the data — and the two
places the record could quietly become useless: a strategy kept only as a
digest, and costs recorded as a reference to a default that will later move.

They also pin the holdout's one-shot property. It is enforced by a unique
constraint rather than a check-then-insert, so it holds against a second
attempt however that attempt arrives.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtest.engine import CostModel, MarketData
from app.backtest.strategies import StrategyDefinition, buy_and_hold, moving_average_cross
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.types import Interval, SampleType
from app.models import Base, Instrument
from app.repositories import backtest_repo, candle_repo
from app.repositories.backtest_repo import CodeVersion
from app.repositories.candle_repo import CandleRow
from app.services import backtest_service as svc
from app.services.backtest_service import RunRequest, StrategySpec
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

CIK = fake_cik(__name__)
CIK_SECOND = fake_cik(__name__ + ".second")

US = MarketCalendar(Market.US)
HISTORY = US.sessions_between(date(2024, 1, 2), date(2024, 12, 31))

# One year and 60/30 windows rather than two years and 120/60. Same properties
# — three windows still tile, still roll, still reserve a holdout — at roughly
# a quarter of the simulation work, which these tests were spending minutes on.
TRAIN = 60
EVAL = 30
HOLDOUT = 30
CODE = CodeVersion(sha="a" * 40, dirty=False)


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
        inst = Instrument(market=Market.US, name="PERSIST TEST CORP", us_cik=CIK)
        s.add(inst)
        s.flush()
        iid = inst.instrument_id

        candle_repo.save_revisions(
            s, [_row(iid, day, Decimal(100 + (i % 97))) for i, day in enumerate(HISTORY)]
        )
        s.commit()

        yield s, iid

        s.execute(text("DELETE FROM backtest_run WHERE instrument_id = :i"), {"i": iid})
        for table in ("candle", "instrument"):
            s.execute(text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": iid})
        s.commit()


def request_for(iid: int) -> RunRequest:
    return RunRequest(
        instrument_id=iid,
        start=HISTORY[0],
        end=HISTORY[-1],
        starting_cash=Decimal("100000"),
        costs=CostModel(Decimal("5"), Decimal("7"), Decimal("1000")),
    )


def run_and_store(
    s: Session, iid: int, spec: StrategySpec | None = None, **kwargs: object
) -> tuple[object, object]:
    report = svc.walk_forward(
        s,
        spec or StrategySpec(definition=moving_average_cross(short=10, long=30)),
        request_for(iid),
        train_sessions=TRAIN,
        eval_sessions=EVAL,
        holdout_sessions=HOLDOUT,
        **kwargs,  # type: ignore[arg-type]
    )
    run = svc.persist(s, report, code=CODE)
    s.commit()
    return report, run


class TestTheThreeAxes:
    def test_the_strategy_is_stored_whole(self, instrument: tuple[Session, int]) -> None:
        """A fingerprint identifies a strategy; only the params rebuild one."""
        s, iid = instrument
        _, run = run_and_store(s, iid)

        assert run.strategy_kind == "moving_average_cross"  # type: ignore[attr-defined]
        assert run.strategy_params == {"short": 10, "long": 30}  # type: ignore[attr-defined]
        assert run.strategy_fingerprint  # type: ignore[attr-defined]

    def test_the_stored_definition_rebuilds_what_ran(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = run_and_store(s, iid)

        rebuilt = StrategyDefinition(
            kind=run.strategy_kind,  # type: ignore[attr-defined]
            version=run.strategy_version,  # type: ignore[attr-defined]
            params=run.strategy_params,  # type: ignore[attr-defined]
        ).build()

        assert (rebuilt.short, rebuilt.long) == (10, 30)  # type: ignore[union-attr]

    def test_the_code_is_stored(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = run_and_store(s, iid)

        assert run.git_commit_sha == CODE.sha  # type: ignore[attr-defined]
        assert run.git_dirty is False  # type: ignore[attr-defined]

    def test_a_dirty_tree_is_recorded_as_such(self, instrument: tuple[Session, int]) -> None:
        """A sha alone does not describe code with uncommitted edits, and the
        row must not look like a clean build."""
        s, iid = instrument
        report = svc.walk_forward(
            s,
            StrategySpec(definition=buy_and_hold()),
            request_for(iid),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
        )
        run = svc.persist(s, report, code=CodeVersion(sha="b" * 40, dirty=True))
        s.commit()

        assert run.git_dirty is True

    def test_the_data_snapshot_is_stored(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report, run = run_and_store(s, iid)

        assert run.data_snapshot_at == report.data_snapshot_at  # type: ignore[attr-defined]


class TestCostsAreExpanded:
    def test_the_applied_values_are_stored_as_numbers(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Not a reference to a default that will later move."""
        s, iid = instrument
        _, run = run_and_store(s, iid)

        assert run.commission_bps == Decimal("5")  # type: ignore[attr-defined]
        assert run.slippage_bps == Decimal("7")  # type: ignore[attr-defined]
        assert run.min_commission == Decimal("1000")  # type: ignore[attr-defined]

    def test_an_unspecified_cost_model_is_stored_as_what_was_applied(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Otherwise the row becomes a different claim when the default moves."""
        s, iid = instrument
        report = svc.walk_forward(
            s,
            StrategySpec(definition=buy_and_hold()),
            RunRequest(
                instrument_id=iid,
                start=HISTORY[0],
                end=HISTORY[-1],
                starting_cash=Decimal("100000"),
            ),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
        )
        run = svc.persist(s, report, code=CODE)
        s.commit()

        default = CostModel()
        assert run.commission_bps == default.commission_bps
        assert run.slippage_bps == default.slippage_bps

    def test_the_execution_model_is_stored(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = run_and_store(s, iid)

        assert run.execution_model == "NEXT_OPEN"  # type: ignore[attr-defined]


class TestWindowsAreRowsNotAverages:
    def test_both_sides_of_every_split_are_stored(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report, run = run_and_store(s, iid)

        stored = backtest_repo.windows_of(s, run.id)  # type: ignore[attr-defined]
        assert len(stored) == len(report.windows)  # type: ignore[attr-defined]
        assert {w.sample_type for w in stored} == {
            SampleType.IN_SAMPLE,
            SampleType.OUT_OF_SAMPLE,
        }

    def test_each_window_records_what_it_ran(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = run_and_store(s, iid)

        for window in backtest_repo.windows_of(s, run.id):  # type: ignore[attr-defined]
            assert window.chosen_params == {"short": 10, "long": 30}

    def test_a_fitted_run_records_each_folds_own_choice(
        self, instrument: tuple[Session, int]
    ) -> None:
        """A run storing only the fitter's name cannot say why a fold
        behaved as it did."""
        s, iid = instrument
        shorts = iter([10, 15, 20, 25, 30, 35, 40])

        def fit(view: MarketData, iid: int, lo: date, hi: date) -> StrategyDefinition:
            return moving_average_cross(short=next(shorts), long=60)

        _, run = run_and_store(s, iid, StrategySpec(fit=fit, fitter_version="alternating@v1"))

        chosen = [
            w.chosen_params["short"]
            for w in backtest_repo.windows_of(s, run.id, sample_type=SampleType.OUT_OF_SAMPLE)  # type: ignore[attr-defined]
        ]
        assert len(set(chosen)) == len(chosen)
        assert run.fitter_version == "alternating@v1"  # type: ignore[attr-defined]

    def test_the_caveats_survive_per_window(self, instrument: tuple[Session, int]) -> None:
        """Abstentions and stale-marked sessions must not be averaged away."""
        s, iid = instrument
        _, run = run_and_store(s, iid)

        first = backtest_repo.windows_of(s, run.id, sample_type=SampleType.IN_SAMPLE)[0]  # type: ignore[attr-defined]
        assert first.abstained == 29
        assert first.without_data == 0

    def test_a_figure_the_sample_could_not_support_is_stored_as_null(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Not as a zero, which reads as a measurement."""
        s, iid = instrument
        _, run = run_and_store(s, iid)

        windows = backtest_repo.windows_of(s, run.id)  # type: ignore[attr-defined]
        assert any(w.profit_factor is None for w in windows)


class TestTheHoldoutBelongsToItsRun:
    """Two ways a run could end up with a holdout that is not its own.

    Reproduced live before the fix, on a stored Apple run:

        holdout rows on run #36: 2
          index=5  2026-06-25..2026-09-18  return +0.219
          index=6  2026-06-26..2026-09-18  return -0.211

    The second was Samsung's, from a different experiment entirely. It fit
    because the slot constraint keyed on window index as well as sample type,
    so a holdout at a different index was a different slot, and because
    storing one checked that it *was* a holdout rather than that it was *this
    run's* holdout.
    """

    def test_walk_forward_persistence_writes_no_holdout(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Having one before anyone decided to look is the failure mode."""
        s, iid = instrument
        _, run = run_and_store(s, iid)

        assert backtest_repo.holdout_of(s, run.id) is None  # type: ignore[attr-defined]

    def test_it_is_written_by_its_own_call(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report, run = run_and_store(s, iid)

        stored = svc.evaluate_and_persist_holdout(s, run, report)  # type: ignore[arg-type]
        s.commit()

        assert stored.sample_type is SampleType.HOLDOUT
        assert backtest_repo.holdout_of(s, run.id) is not None  # type: ignore[attr-defined]

    def test_a_second_one_is_refused(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report, run = run_and_store(s, iid)

        svc.evaluate_and_persist_holdout(s, run, report)  # type: ignore[arg-type]
        s.commit()

        with pytest.raises(backtest_repo.HoldoutAlreadyRecordedError):
            svc.evaluate_and_persist_holdout(s, run, report)  # type: ignore[arg-type]

    def test_a_second_one_at_a_different_index_is_also_refused(
        self, instrument: tuple[Session, int]
    ) -> None:
        """The slot constraint alone allowed this; a partial unique index
        on (run_id) where HOLDOUT is what actually forbids it."""
        s, iid = instrument
        report, run = run_and_store(s, iid)
        first = svc.evaluate_holdout(s, report)  # type: ignore[arg-type]

        backtest_repo.save_window(
            s,
            run,  # type: ignore[arg-type]
            window_index=first.index,
            sample_type=SampleType.HOLDOUT,
            period_start=first.start,
            period_end=first.end,
            chosen=first.chosen,
            sessions=first.sessions,
            observations=0,
            total_return=None,
            cagr=None,
            max_drawdown=None,
            sharpe=None,
            win_rate=None,
            profit_factor=None,
            trades=0,
            abstained=0,
            without_data=0,
            unfilled=0,
        )
        s.commit()

        with pytest.raises(backtest_repo.HoldoutAlreadyRecordedError):
            backtest_repo.save_window(
                s,
                run,  # type: ignore[arg-type]
                window_index=first.index + 1,  # a different slot
                sample_type=SampleType.HOLDOUT,
                period_start=first.start,
                period_end=first.end,
                chosen=first.chosen,
                sessions=first.sessions,
                observations=0,
                total_return=None,
                cagr=None,
                max_drawdown=None,
                sharpe=None,
                win_rate=None,
                profit_factor=None,
                trades=0,
                abstained=0,
                without_data=0,
                unfilled=0,
            )

    def test_only_one_row_survives(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report, run = run_and_store(s, iid)

        svc.evaluate_and_persist_holdout(s, run, report)  # type: ignore[arg-type]
        s.commit()
        with pytest.raises(backtest_repo.HoldoutAlreadyRecordedError):
            svc.evaluate_and_persist_holdout(s, run, report)  # type: ignore[arg-type]
        s.rollback()

        rows = backtest_repo.windows_of(s, run.id, sample_type=SampleType.HOLDOUT)  # type: ignore[attr-defined]
        assert len(rows) == 1

    def test_another_experiments_holdout_is_refused(self, instrument: tuple[Session, int]) -> None:
        """The live failure: a different instrument's holdout on this run."""
        s, iid = instrument
        _, run = run_and_store(s, iid)

        other = Instrument(market=Market.US, name="OTHER CORP", us_cik=CIK_SECOND)
        s.add(other)
        s.flush()
        candle_repo.save_revisions(
            s,
            [
                _row(other.instrument_id, day, Decimal(200 + (i % 53)))
                for i, day in enumerate(HISTORY)
            ],
        )
        s.commit()

        foreign = svc.walk_forward(
            s,
            StrategySpec(definition=moving_average_cross(short=10, long=30)),
            request_for(other.instrument_id),
            train_sessions=TRAIN,
            eval_sessions=EVAL,
            holdout_sessions=HOLDOUT,
        )

        with pytest.raises(svc.HoldoutError, match="instrument"):
            svc.evaluate_and_persist_holdout(s, run, foreign)  # type: ignore[arg-type]

        s.rollback()
        s.execute(
            text("DELETE FROM candle WHERE instrument_id = :i"),
            {"i": other.instrument_id},
        )
        s.execute(
            text("DELETE FROM instrument WHERE instrument_id = :i"),
            {"i": other.instrument_id},
        )
        s.commit()

    def test_a_changed_split_is_refused(self, instrument: tuple[Session, int]) -> None:
        """Same instrument, different experiment — still not this run."""
        s, iid = instrument
        _, run = run_and_store(s, iid)

        different = svc.walk_forward(
            s,
            StrategySpec(definition=moving_average_cross(short=10, long=30)),
            request_for(iid),
            train_sessions=TRAIN - 20,  # a different split
            eval_sessions=EVAL,
            holdout_sessions=HOLDOUT,
        )

        with pytest.raises(svc.HoldoutError, match="train sessions"):
            svc.evaluate_and_persist_holdout(s, run, different)  # type: ignore[arg-type]


class TestProvenanceCannotBeSkipped:
    def test_a_commit_is_resolvable_here(self) -> None:
        """This repository is a git checkout, so the real path works."""
        version = backtest_repo.resolve_commit()

        assert len(version.sha) == 40
        assert all(c in "0123456789abcdef" for c in version.sha)

    def test_a_missing_repository_raises_rather_than_storing_unknown(
        self, tmp_path: object
    ) -> None:
        """'unknown' in that column looks like a value and destroys the axis."""
        with pytest.raises(backtest_repo.ProvenanceError, match="cannot resolve"):
            backtest_repo.resolve_commit(tmp_path)  # type: ignore[arg-type]
