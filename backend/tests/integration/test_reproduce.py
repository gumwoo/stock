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

import hashlib
import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtest import strategies
from app.backtest.engine import CostModel, MarketData
from app.backtest.strategies import (
    StrategyDefinition,
    buy_and_hold,
    moving_average_cross,
)
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
        self, instrument: tuple[Session, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run that referenced a kind since removed cannot be checked, and
        saying so is different from saying it failed to reproduce.

        The kind is removed from this build rather than rewritten in the row:
        rewriting it would make the row internally inconsistent, and the
        integrity step would — correctly — report tampering instead. This is
        the other case, a faithful record of a rule the code no longer has.
        """
        s, iid = instrument
        _, run = store(s, iid)

        kinds = dict(strategies._KINDS)
        kinds.pop("moving_average_cross")
        monkeypatch.setattr(strategies, "_KINDS", kinds)

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


class TestTheRowsMustBeTheOnesThatWereWritten:
    """Re-running proves the engine agrees with the rows. Not that the rows
    are the ones that were stored.

    Replaying a tampered row and comparing against that row's own figures
    agrees with itself perfectly. Probed against the live database before the
    integrity step existed — every one of these reported success:

        run.strategy_params changed, fingerprint stale   reproduced=True
        run.fit_trace_fingerprint no longer matches      reproduced=True
        one window row deleted                           reproduced=True
        run.holdout_start/end changed after the fact     reproduced=True

    The deletion is the one that should worry anybody: drop the window that
    did worst and the run still reports as reproduced.
    """

    def test_a_consistently_rewritten_window_is_still_caught(
        self, instrument: tuple[Session, int]
    ) -> None:
        """The case a metric comparison cannot see.

        The recorded strategy and every figure are replaced together, so the
        row is self-consistent and replays to exactly what it claims. Only the
        run header remembers which experiment this was.
        """
        s, iid = instrument
        _, run = store(s, iid)

        rewritten = svc.walk_forward(
            s,
            StrategySpec(definition=moving_average_cross(short=20, long=50)),
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
        other = svc.persist(s, rewritten, code=CODE)
        s.commit()

        # Move the second run's window rows onto the first run, so they are
        # internally consistent and replay correctly — but are not what the
        # first run's header describes.
        s.execute(text("DELETE FROM backtest_window WHERE run_id = :r"), {"r": run.id})
        s.execute(
            text("UPDATE backtest_window SET run_id = :r WHERE run_id = :o"),
            {"r": run.id, "o": other.id},
        )
        s.commit()
        s.expire_all()

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert any("fit trace" in finding for finding in result.integrity)

    def test_a_deleted_window_is_caught(self, instrument: tuple[Session, int]) -> None:
        """Dropping the worst fold must not leave a run that reproduces."""
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text(
                "DELETE FROM backtest_window WHERE run_id = :r AND window_index = 1 "
                "AND sample_type = 'OUT_OF_SAMPLE'"
            ),
            {"r": run.id},
        )
        s.commit()
        s.expire_all()

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert any("fit trace" in finding for finding in result.integrity)

    def test_a_moved_window_period_is_caught(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text(
                "UPDATE backtest_window SET period_start = period_start - 7, "
                "period_end = period_end - 7 WHERE run_id = :r AND window_index = 0 "
                "AND sample_type = 'IN_SAMPLE'"
            ),
            {"r": run.id},
        )
        s.commit()
        s.expire_all()

        assert not rs.reproduce(s, run.id).reproduced

    def test_a_stale_header_fingerprint_is_caught(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text(
                "UPDATE backtest_run SET strategy_params = "
                '\'{"short": 99, "long": 200}\'::jsonb WHERE id = :r'
            ),
            {"r": run.id},
        )
        s.commit()
        s.expire_all()

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert any("strategy fingerprint" in finding for finding in result.integrity)

    def test_a_stale_window_fingerprint_is_caught(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text(
                "UPDATE backtest_window SET chosen_fingerprint = 'deadbeefdeadbeef' "
                "WHERE run_id = :r AND window_index = 0"
            ),
            {"r": run.id},
        )
        s.commit()
        s.expire_all()

        assert not rs.reproduce(s, run.id).reproduced

    def test_moved_holdout_dates_are_caught(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text(
                "UPDATE backtest_run SET holdout_start = '2020-01-02', "
                "holdout_end = '2020-03-01' WHERE id = :r"
            ),
            {"r": run.id},
        )
        s.commit()
        s.expire_all()

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert any("holdout" in finding for finding in result.integrity)

    def test_an_untouched_run_has_no_findings(self, instrument: tuple[Session, int]) -> None:
        """Otherwise every check above passes by failing everything."""
        s, iid = instrument
        _, run = store(s, iid)

        result = rs.reproduce(s, run.id)

        assert result.integrity == ()
        assert result.reproduced

    def test_findings_stop_the_replay_rather_than_being_lost_to_it(
        self, instrument: tuple[Session, int]
    ) -> None:
        """A tampered period used to raise out of the replay before the
        findings could be reported at all."""
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text(
                "UPDATE backtest_window SET period_start = '2019-01-02', "
                "period_end = '2019-03-01' WHERE run_id = :r AND window_index = 0 "
                "AND sample_type = 'IN_SAMPLE'"
            ),
            {"r": run.id},
        )
        s.commit()
        s.expire_all()

        result = rs.reproduce(s, run.id)

        assert result.integrity
        assert result.windows == ()
        assert not result.reproduced


class TestTheHoldoutIsCheckedToo:
    """The final verdict of an experiment was the one figure nobody checked.

        UPDATE backtest_window SET total_return = 99, sharpe = 999,
               trades = 999 WHERE sample_type = 'HOLDOUT';

        reproduced = True

    Replaying it is not re-evaluating it. Nothing is chosen and no fitter
    runs: the row's own strategy is re-executed over the row's own period to
    check the figures were recorded correctly, which is a different act from
    taking the measurement.
    """

    def test_a_stored_holdout_is_replayed(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report, run = store(s, iid)
        svc.evaluate_and_persist_holdout(s, run, report)
        s.commit()

        result = rs.reproduce(s, run.id)

        assert any(w.sample_type is SampleType.HOLDOUT for w in result.windows)
        assert result.reproduced

    def test_altering_its_figures_is_caught(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report, run = store(s, iid)
        svc.evaluate_and_persist_holdout(s, run, report)
        s.commit()

        s.execute(
            text(
                "UPDATE backtest_window SET total_return = 99, sharpe = 999, trades = 999 "
                "WHERE run_id = :r AND sample_type = 'HOLDOUT'"
            ),
            {"r": run.id},
        )
        s.commit()
        s.expire_all()

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert result.mismatches[0].sample_type is SampleType.HOLDOUT

    def test_a_run_without_one_is_unaffected(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        result = rs.reproduce(s, run.id)

        assert result.reproduced
        assert all(w.sample_type is not SampleType.HOLDOUT for w in result.windows)


class TestTheHeaderMustDescribeTheWindows:
    """Both fingerprints can be recomputed and still disagree with each other.

    Rewrite the header's strategy, recompute its digest, and every earlier
    check passes while the header describes a different experiment from the
    one the rows record. Probed live: `reproduced=True`.
    """

    def test_a_relabelled_header_is_caught(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        params = {"short": 20, "long": 60}
        canonical = json.dumps(
            {"kind": "moving_average_cross", "version": "ma-20-60@v1", "params": params},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        s.execute(
            text(
                "UPDATE backtest_run SET strategy_version = 'ma-20-60@v1', "
                "strategy_params = cast(:p as jsonb), strategy_fingerprint = :f "
                "WHERE id = :r"
            ),
            {
                "p": json.dumps(params),
                "f": hashlib.sha256(canonical.encode()).hexdigest()[:16],
                "r": run.id,
            },
        )
        s.commit()
        s.expire_all()

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert any("every window ran" in finding for finding in result.integrity)

    def test_a_fixed_run_relabelled_as_fitted_is_caught(
        self, instrument: tuple[Session, int]
    ) -> None:
        """`fitted` decides whether an in/out gap means anything about
        overfitting, so inventing a fitter changes what the run claims."""
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text("UPDATE backtest_run SET fitter_version = 'never-happened@v1' WHERE id = :r"),
            {"r": run.id},
        )
        s.commit()
        s.expire_all()

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert any("fitter" in finding for finding in result.integrity)

    def test_a_fitted_run_stripped_of_its_fitter_is_caught(
        self, instrument: tuple[Session, int]
    ) -> None:
        """The other direction: a run whose folds each chose differently
        cannot claim to have run one fixed rule."""
        s, iid = instrument
        shorts = iter([10, 15, 20, 25, 30, 35])

        def fit(view: MarketData, i: int, lo: date, hi: date) -> StrategyDefinition:
            return moving_average_cross(short=next(shorts), long=40)

        _, run = store(s, iid, StrategySpec(fit=fit, fitter_version="grid@v1"))

        s.execute(
            text("UPDATE backtest_run SET fitter_version = NULL WHERE id = :r"),
            {"r": run.id},
        )
        s.commit()
        s.expire_all()

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert any("different ones" in finding for finding in result.integrity)

    def test_a_genuine_fitted_run_passes(self, instrument: tuple[Session, int]) -> None:
        """Otherwise the checks above pass by rejecting every fitted run."""
        s, iid = instrument
        shorts = iter([10, 15, 20, 25, 30, 35])

        def fit(view: MarketData, i: int, lo: date, hi: date) -> StrategyDefinition:
            return moving_average_cross(short=next(shorts), long=40)

        _, run = store(s, iid, StrategySpec(fit=fit, fitter_version="grid@v1"))

        assert rs.reproduce(s, run.id).reproduced


class TestTheHoldoutStrategyIsAnchored:
    """The fit trace covers the measured windows and deliberately not the
    holdout, because a fitted run's final refit need not match any fold's
    choice. That left the holdout's own strategy tied to nothing:

        fixed run, holdout swapped to buy_and_hold   reproduced=True
        fitted run, holdout swapped to buy_and_hold  reproduced=True

    Rewrite the holdout row to another strategy, recompute its fingerprint and
    its twelve measurements, and it replays to exactly what it now claims —
    leaving the experiment's final verdict a measurement of something it never
    ran.
    """

    @staticmethod
    def _swap(s: Session, run_id: int, definition: StrategyDefinition) -> None:
        s.execute(
            text(
                "UPDATE backtest_window SET chosen_kind = :k, chosen_version = :v, "
                "chosen_params = cast(:p as jsonb), chosen_fingerprint = :f "
                "WHERE run_id = :r AND sample_type = 'HOLDOUT'"
            ),
            {
                "k": definition.kind,
                "v": definition.version,
                "p": json.dumps(dict(definition.params)),
                "f": definition.fingerprint,
                "r": run_id,
            },
        )
        s.commit()
        s.expire_all()

    def test_the_run_records_which_strategy_took_the_measurement(
        self, instrument: tuple[Session, int]
    ) -> None:
        s, iid = instrument
        report, run = store(s, iid)
        svc.evaluate_and_persist_holdout(s, run, report)
        s.commit()

        holdout = backtest_repo.holdout_of(s, run.id)

        assert holdout is not None
        assert run.holdout_strategy_fingerprint == holdout.chosen_fingerprint

    def test_it_is_null_until_a_holdout_is_taken(self, instrument: tuple[Session, int]) -> None:
        """A run has no holdout until one is evaluated, deliberately."""
        s, iid = instrument
        _, run = store(s, iid)

        assert run.holdout_strategy_fingerprint is None
        assert rs.reproduce(s, run.id).reproduced

    def test_a_swapped_fixed_holdout_is_caught(self, instrument: tuple[Session, int]) -> None:
        s, iid = instrument
        report, run = store(s, iid)
        svc.evaluate_and_persist_holdout(s, run, report)
        s.commit()

        self._swap(s, run.id, buy_and_hold())

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert any("holdout row ran" in finding for finding in result.integrity)

    def test_a_swapped_fitted_holdout_is_caught(self, instrument: tuple[Session, int]) -> None:
        """The case the fit trace cannot cover."""
        s, iid = instrument
        shorts = iter([10, 15, 20, 25, 30, 35])

        def fit(view: MarketData, i: int, lo: date, hi: date) -> StrategyDefinition:
            return moving_average_cross(short=next(shorts), long=40)

        report, run = store(s, iid, StrategySpec(fit=fit, fitter_version="grid@v1"))
        svc.evaluate_and_persist_holdout(s, run, report)
        s.commit()

        self._swap(s, run.id, buy_and_hold())

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert any("holdout row ran" in finding for finding in result.integrity)

    def test_a_fixed_run_whose_holdout_differs_from_its_header_is_caught(
        self, instrument: tuple[Session, int]
    ) -> None:
        """Even with the anchor rewritten to agree with the tampered row."""
        s, iid = instrument
        report, run = store(s, iid)
        svc.evaluate_and_persist_holdout(s, run, report)
        s.commit()

        swapped = buy_and_hold()
        self._swap(s, run.id, swapped)
        s.execute(
            text("UPDATE backtest_run SET holdout_strategy_fingerprint = :f WHERE id = :r"),
            {"f": swapped.fingerprint, "r": run.id},
        )
        s.commit()
        s.expire_all()

        result = rs.reproduce(s, run.id)

        assert not result.reproduced
        assert any("ran the header's strategy" in finding for finding in result.integrity)

    def test_a_holdout_with_no_recorded_strategy_is_caught(
        self, instrument: tuple[Session, int]
    ) -> None:
        s, iid = instrument
        report, run = store(s, iid)
        svc.evaluate_and_persist_holdout(s, run, report)
        s.commit()

        s.execute(
            text("UPDATE backtest_run SET holdout_strategy_fingerprint = NULL WHERE id = :r"),
            {"r": run.id},
        )
        s.commit()
        s.expire_all()

        assert not rs.reproduce(s, run.id).reproduced

    def test_a_recorded_strategy_with_no_holdout_is_caught(
        self, instrument: tuple[Session, int]
    ) -> None:
        s, iid = instrument
        _, run = store(s, iid)

        s.execute(
            text(
                "UPDATE backtest_run SET holdout_strategy_fingerprint = 'deadbeefdeadbeef' "
                "WHERE id = :r"
            ),
            {"r": run.id},
        )
        s.commit()
        s.expire_all()

        assert not rs.reproduce(s, run.id).reproduced
