"""A stored run, run again.

Everything before this made a run describable. This is what makes the
description worth anything — and it is the first test in the project that can
fail because a *record* is wrong rather than because a calculation is.

The tamper test is the one that matters. Reproducing a run that was stored
correctly proves the machinery runs; reproducing one whose stored figures were
altered proves the comparison can tell. Without the second, a reproduction
that silently agreed with everything would look exactly like success.
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
from app.models.backtest import BacktestWindow
from app.repositories import backtest_repo, candle_repo
from app.repositories.backtest_repo import CodeVersion
from app.repositories.candle_repo import CandleRow
from app.services import backtest_service as svc
from app.services import reproduce_service as rs
from app.services.backtest_service import RunRequest, StrategySpec
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

CIK = fake_cik(__name__)
US = MarketCalendar(Market.US)
HISTORY = US.sessions_between(date(2024, 1, 2), date(2024, 12, 31))
TRAIN, EVAL, HOLDOUT = 60, 30, 30
CODE = CodeVersion(sha="c" * 40, dirty=False)


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
        inst = Instrument(market=Market.US, name="REPRODUCE CORP", us_cik=CIK)
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


def store(s: Session, iid: int, spec: StrategySpec | None = None):
    report = svc.walk_forward(
        s,
        spec or StrategySpec(definition=moving_average_cross(short=10, long=30)),
        RunRequest(
            instrument_id=iid,
            start=HISTORY[0],
            end=HISTORY[-1],
            starting_cash=Decimal("100000"),
            costs=CostModel(Decimal("5"), Decimal("7"), Decimal("0")),
        ),
        train_sessions=TRAIN,
        eval_sessions=EVAL,
        holdout_sessions=HOLDOUT,
    )
    run = svc.persist(s, report, code=CODE)
    s.commit()
    return report, run


class TestAStoredRunComesBackTheSame:
    def test_every_window_reproduces(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        result = rs.reproduce(s, run.id)

        assert result.reproduced
        assert result.mismatches == ()

    def test_it_compares_every_stored_window(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report, run = store(s, iid)

        result = rs.reproduce(s, run.id)

        assert len(result.windows) == len(report.windows)

    def test_the_holdout_is_not_replayed(self, instrument: tuple[Session, int]) -> None:
        """It is a measurement taken once; re-running it is not reproducing
        the walk-forward, and its row is not part of the experiment."""
        s, iid = instrument
        report, run = store(s, iid)
        svc.evaluate_and_persist_holdout(s, run, report)
        s.commit()

        result = rs.reproduce(s, run.id)

        assert all(w.sample_type is not SampleType.HOLDOUT for w in result.windows)

    def test_a_fitted_run_replays_each_folds_own_choice(
        self, instrument: tuple[Session, int]
    ) -> None:
        """The header cannot rebuild a fitter — that is code, not data — so
        the window rows are what a fitted run is reproduced from."""
        s, iid = instrument
        shorts = iter([10, 15, 20, 25, 30, 35])

        def fit(view: MarketData, i: int, lo: date, hi: date) -> StrategyDefinition:
            return moving_average_cross(short=next(shorts), long=40)

        _, run = store(s, iid, StrategySpec(fit=fit, fitter_version="grid@v1"))

        result = rs.reproduce(s, run.id)

        assert result.reproduced

    def test_running_it_twice_agrees(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        assert rs.reproduce(s, run.id).windows == rs.reproduce(s, run.id).windows


class TestItCanTellWhenSomethingChanged:
    """Without this, agreeing with everything would look like success.

    An earlier version compared the total return and the trade count only, so
    every other figure in the row could be anything at all:

        UPDATE backtest_window SET sharpe = 999, max_drawdown = 0.99,
               abstained = 999, unfilled = 999, without_data = 999,
               observations = 1, sessions = 1, cagr = 42, win_rate = 1,
               profit_factor = 77;

        reproduced = True   mismatches = 0

    The caveat columns matter most. A run whose abstentions or stale-marked
    sessions were wrong describes a different experiment, and those are the
    figures a summary would never show.
    """

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("total_return", "0.99"),
            ("trades", "77"),
            ("sharpe", "999"),
            ("max_drawdown", "0.99"),
            ("cagr", "42"),
            ("win_rate", "1"),
            ("profit_factor", "77"),
            ("sessions", "1"),
            ("observations", "1"),
            ("abstained", "999"),
            ("without_data", "999"),
            ("unfilled", "999"),
        ],
    )
    def test_altering_any_stored_measurement_is_caught(
        self, instrument: tuple[Session, int], column: str, value: str
    ) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text(
                f"UPDATE backtest_window SET {column} = {value} "
                "WHERE run_id = :r AND window_index = 0 AND sample_type = 'IN_SAMPLE'"
            ),
            {"r": run.id},
        )
        s.commit()

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert column in result.mismatches[0].differences

    def test_every_stored_measurement_is_compared(self, instrument: tuple[Session, int]) -> None:
        """The guard that keeps this from decaying: a figure added to the
        window table must be compared, not forgotten."""
        stored = {c.name for c in BacktestWindow.__table__.columns}
        identity = {
            "id",
            "run_id",
            "window_index",
            "sample_type",
            "period_start",
            "period_end",
            "chosen_kind",
            "chosen_version",
            "chosen_params",
            "chosen_fingerprint",
            "ingested_at",
        }

        assert stored - identity - set(rs.MEASUREMENTS) == set()

    def test_an_altered_recorded_strategy_is_caught(self, instrument: tuple[Session, int]) -> None:
        """Replaying from the row means a wrong row replays wrongly."""
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text(
                "UPDATE backtest_window SET chosen_kind = 'buy_and_hold', "
                "chosen_params = '{}'::jsonb WHERE run_id = :r"
            ),
            {"r": run.id},
        )
        s.commit()

        assert not rs.reproduce(s, run.id).reproduced

    def test_the_mismatch_says_what_moved(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text(
                "UPDATE backtest_window SET total_return = 0.99 "
                "WHERE run_id = :r AND window_index = 0 AND sample_type = 'IN_SAMPLE'"
            ),
            {"r": run.id},
        )
        s.commit()

        described = rs.reproduce(s, run.id).mismatches[0].describe()

        assert "total_return" in described
        assert "+0.990000" in described
        assert "->" in described


class TestWhatItRefusesToAttempt:
    def test_an_unknown_run_raises(self, instrument: tuple[Session, int]) -> None:
        s, _ = instrument
        with pytest.raises(rs.ReproduceError, match="no backtest run"):
            rs.reproduce(s, 10_000_000)

    def test_a_run_with_no_windows_raises(self, instrument: tuple[Session, int]) -> None:
        """Nothing to compare against is not a successful reproduction."""
        s, iid = instrument
        _, run = store(s, iid)
        s.execute(text("DELETE FROM backtest_window WHERE run_id = :r"), {"r": run.id})
        s.commit()

        with pytest.raises(rs.ReproduceError, match="nothing to compare"):
            rs.reproduce(s, run.id)

    def test_a_strategy_this_build_cannot_construct_raises(
        self, instrument: tuple[Session, int]
    ) -> None:
        """A run that referenced a kind since removed cannot be checked, and
        saying so is different from saying it failed to reproduce."""
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text("UPDATE backtest_window SET chosen_kind = 'retired_rule' WHERE run_id = :r"),
            {"r": run.id},
        )
        s.commit()

        with pytest.raises(rs.ReproduceError, match="cannot construct"):
            rs.reproduce(s, run.id)


class TestTheCodeIsReportedNotEnforced:
    def test_a_differing_commit_is_reported(self, instrument: tuple[Session, int]) -> None:
        """A run reproduced from different code is still worth reproducing:
        if the numbers match anyway that is information, and if they do not,
        the commit is the first place to look."""
        s, iid = instrument
        _, run = store(s, iid)

        result = rs.reproduce(s, run.id)

        assert result.stored_commit == CODE.sha
        assert result.code_matches is False
        assert "code differs" in result.summary()

    def test_the_numbers_are_still_compared(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        assert rs.reproduce(s, run.id).reproduced

    def test_a_dirty_stored_run_says_so(self, instrument: tuple[Session, int]) -> None:
        """Its commit does not describe the code that produced it."""
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
        run = svc.persist(s, report, code=CodeVersion(sha="d" * 40, dirty=True))
        s.commit()

        assert "uncommitted changes" in rs.reproduce(s, run.id).summary()


class TestTheCommitIsCapturedWhenTheRunStarts:
    def test_the_report_carries_it(self, instrument: tuple[Session, int]) -> None:
        """Resolving at persist time would record whatever HEAD happened to be
        once a long run finished."""
        s, iid = instrument
        report, _ = store(s, iid)

        assert report.code.sha
        assert report.started_at is not None

    def test_persisting_later_stores_the_captured_one(
        self, instrument: tuple[Session, int]
    ) -> None:
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
        run = svc.persist(s, report)
        s.commit()

        assert run.git_commit_sha == report.code.sha
        assert run.started_at == report.started_at

    def test_it_is_resolved_once_per_process(self) -> None:
        """Python imported these modules before any run began, so editing
        files afterwards does not change the code that is executing."""
        first = backtest_repo.resolve_commit()
        second = backtest_repo.resolve_commit()

        assert first is second


class TestTheCodeVersionSettlesAtImport:
    """Resolving lazily left a window where the answer could change.

    Start the process on commit A, check out B, then run the first backtest:
    the row would say B while the objects executing came from A. Python
    imported the modules once, so the truthful value is the one at import.
    """

    def test_it_is_the_same_object_every_time(self) -> None:
        assert backtest_repo.resolve_commit() is backtest_repo.resolve_commit()

    def test_an_injected_sha_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """How a container should carry this. A deployed image has no `.git`,
        and provenance that depends on one disappears where it matters most."""
        monkeypatch.setenv("GIT_SHA", "e" * 40)
        monkeypatch.setenv("GIT_DIRTY", "1")

        resolved = backtest_repo._resolve_at_import()

        assert isinstance(resolved, CodeVersion)
        assert resolved.sha == "e" * 40
        assert resolved.dirty is True

    def test_a_missing_repository_is_an_error_not_a_placeholder(self, tmp_path: object) -> None:
        """'unknown' in that column looks like a value."""
        with pytest.raises(backtest_repo.ProvenanceError, match="cannot resolve"):
            backtest_repo.resolve_commit(tmp_path)  # type: ignore[arg-type]

    def test_a_failure_is_carried_rather_than_breaking_the_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unimportable module is worse than a run that cannot be stored."""
        monkeypatch.delenv("GIT_SHA", raising=False)
        monkeypatch.setattr(
            backtest_repo,
            "_read_git",
            lambda root: (_ for _ in ()).throw(backtest_repo.ProvenanceError("no git here")),
        )

        assert isinstance(backtest_repo._resolve_at_import(), backtest_repo.ProvenanceError)
