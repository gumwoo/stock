"""The forward record: entries, exits and what the report counts.

Bars are laid on real Korean sessions from August 2026, one per session, so
the calendar arithmetic is the production one. Close on the i-th session is
100 + i and the open half a point below, which makes every expected return a
number that can be written down.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.types import Freshness, MissingFactorPolicy, SignalAction
from app.models import (
    Base,
    CandidateOutcome,
    CandidateSnapshot,
    Instrument,
    Interval,
    Signal,
    SignalOutcome,
    SignalRegime,
    SymbolHistory,
)
from app.models.collector import CollectorStatus
from app.repositories import candle_repo
from app.scoring.regime import Label, Regime
from app.services import discovery_service, forward_service, regime_service
from app.services.discovery_service import Candidate, Discovery

pytestmark = pytest.mark.integration

KR = MarketCalendar(Market.KR)
SESSIONS = KR.sessions_between(date(2026, 8, 3), date(2026, 9, 30))[:40]
NAMES = ("쀓포워드가", "쀓포워드나")


@pytest.fixture(scope="module")
def engine() -> Iterator[object]:
    eng = create_engine(get_settings().database_url, future=True)
    try:
        with eng.connect():
            pass
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


class World:
    def __init__(self, session: Session, ids: list[int]) -> None:
        self.session = session
        self.ids = ids


def bars(instrument_id: int, *, drift: float) -> list[Any]:
    rows = []
    for i, day in enumerate(SESSIONS):
        close = Decimal(str(100 + i * drift))
        rows.append(
            {
                "instrument_id": instrument_id,
                "interval": Interval.DAY_1,
                "ts": KR.session_open(day),
                "available_at": KR.session_close(day),
                "open": close - Decimal("0.5"),
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": Decimal(1000),
                "source": "TEST",
            }
        )
    return rows


@pytest.fixture
def world(engine: object) -> Iterator[World]:
    """Two Korean names: one rising a point a session, one flat."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        ids = []
        for n, name in enumerate(NAMES):
            inst = Instrument(market=Market.KR, name=name, tracked=True)
            s.add(inst)
            s.flush()
            s.add(
                SymbolHistory(
                    instrument_id=inst.instrument_id,
                    symbol=f"99098{n}",
                    valid_from=datetime(2000, 1, 1, tzinfo=UTC).date(),
                    source="SEED",
                )
            )
            ids.append(inst.instrument_id)
        s.commit()
        candle_repo.save_revisions(s, bars(ids[0], drift=1.0) + bars(ids[1], drift=0.0))
        s.commit()
        try:
            yield World(s, ids)
        finally:
            s.rollback()
            for instrument_id in ids:
                for table in ("signal", "candle", "candidate_snapshot", "symbol_history"):
                    s.execute(
                        text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": instrument_id}
                    )
                s.execute(
                    text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": instrument_id}
                )
            s.commit()


VERSION = "forward-test"


def signal(
    world: World,
    instrument_id: int,
    *,
    day: int,
    action: SignalAction,
    version: str = VERSION,
    late: bool = False,
) -> Signal:
    decided = KR.session_close(SESSIONS[day])
    row = Signal(
        instrument_id=instrument_id,
        data_asof=KR.session_close(SESSIONS[day]),
        decision_at=KR.session_close(SESSIONS[day]),
        earliest_execution_at=KR.session_open(SESSIONS[day + 1]),
        total_score=60.0,
        action=action,
        policy=MissingFactorPolicy.ZERO,
        reasons=[],
        strategy_version=version,
        # Written at the close it judges, as the scheduled loop does; `late`
        # writes it after the next open instead.
        ingested_at=KR.session_open(SESSIONS[day + 1]) + timedelta(hours=1) if late else decided,
    )
    world.session.add(row)
    world.session.commit()
    return row


def outcomes(world: World, signal_id: int) -> dict[int, SignalOutcome]:
    world.session.expire_all()
    return {
        o.horizon_sessions: o
        for o in world.session.execute(
            select(SignalOutcome).where(SignalOutcome.signal_id == signal_id)
        ).scalars()
    }


NOW = KR.session_close(SESSIONS[-1]) + timedelta(days=1)


class TestSignalOutcomes:
    def test_entry_is_the_next_open_and_exit_the_hth_close(self, world: World) -> None:
        row = signal(world, world.ids[0], day=0, action=SignalAction.BUY_INTEREST)

        forward_service.evaluate_signals(world.session, now=NOW)

        got = outcomes(world, row.id)
        assert set(got) == {1, 5, 20}
        # Entry: session 1's open, 100.5. Exit after 5 sessions: session 5's close, 105.
        assert got[1].entry_price == pytest.approx(100.5)
        assert got[1].exit_price == pytest.approx(101)
        assert got[5].exit_price == pytest.approx(105)
        assert got[5].return_pct == pytest.approx((105 / 100.5 - 1) * 100)
        assert got[20].exit_at == KR.session_close(SESSIONS[20])

    def test_a_horizon_not_yet_closed_waits(self, world: World) -> None:
        row = signal(world, world.ids[0], day=0, action=SignalAction.WATCH)
        early = KR.session_close(SESSIONS[5]) + timedelta(minutes=1)

        forward_service.evaluate_signals(world.session, now=early)
        assert set(outcomes(world, row.id)) == {1, 5}

        forward_service.evaluate_signals(world.session, now=NOW)
        assert set(outcomes(world, row.id)) == {1, 5, 20}

    def test_an_abstention_has_nothing_to_measure(self, world: World) -> None:
        row = signal(world, world.ids[0], day=0, action=SignalAction.ABSTAINED)
        forward_service.evaluate_signals(world.session, now=NOW)
        assert outcomes(world, row.id) == {}

    def test_running_twice_adds_nothing(self, world: World) -> None:
        signal(world, world.ids[0], day=0, action=SignalAction.WATCH)
        forward_service.evaluate_signals(world.session, now=NOW)
        assert forward_service.evaluate_signals(world.session, now=NOW) == 0

    def test_no_entry_bar_no_outcome(self, world: World) -> None:
        row = signal(world, world.ids[0], day=0, action=SignalAction.WATCH)
        world.session.execute(
            text("DELETE FROM candle WHERE instrument_id = :i AND ts = :t"),
            {"i": world.ids[0], "t": KR.session_open(SESSIONS[1])},
        )
        world.session.commit()
        forward_service.evaluate_signals(world.session, now=NOW)
        assert outcomes(world, row.id) == {}


class TestTheReport:
    def test_excess_is_against_the_same_days_judged_names(self, world: World) -> None:
        signal(world, world.ids[0], day=0, action=SignalAction.BUY_INTEREST)
        signal(world, world.ids[1], day=0, action=SignalAction.CAUTION)
        forward_service.evaluate_signals(world.session, now=NOW)

        rep = forward_service.report(world.session, strategy_version=VERSION)

        rising = (105 / 100.5 - 1) * 100
        flat = (100 / 99.5 - 1) * 100
        buy, caution = rep.by_action[5]["BUY_INTEREST"], rep.by_action[5]["CAUTION"]
        assert buy.mean == pytest.approx(rising)
        assert buy.mean_excess == pytest.approx(rising - (rising + flat) / 2)
        assert caution.mean_excess == pytest.approx(flat - (rising + flat) / 2)
        assert rep.by_overlay[5]["no overlay"].n == 2
        assert rep.by_regime[5]["no regime"].n == 2

    def test_each_judgement_is_counted_under_the_regime_filed_beside_it(self, world: World) -> None:
        first = signal(world, world.ids[0], day=0, action=SignalAction.BUY_INTEREST)
        second = signal(world, world.ids[1], day=0, action=SignalAction.CAUTION)
        world.session.add(
            SignalRegime(
                signal_id=first.id,
                asof=first.decision_at,
                regime_version=regime_service.PARAMS.version,
                index_code="^KS11",
                label="RISK_OFF",
            )
        )
        # Filed under other thresholds: a different grouping, not counted in this one.
        world.session.add(
            SignalRegime(
                signal_id=second.id,
                asof=second.decision_at,
                regime_version=regime_service.PARAMS.version + 1,
                index_code="^KS11",
                label="RISK_ON",
            )
        )
        world.session.commit()
        forward_service.evaluate_signals(world.session, now=NOW)

        rep = forward_service.report(world.session, strategy_version=VERSION)
        assert rep.by_regime[5]["RISK_OFF"].n == 1
        assert rep.by_regime[5]["RISK_OFF"].mean == pytest.approx((105 / 100.5 - 1) * 100)
        assert rep.by_regime[5]["no regime"].n == 1
        assert "RISK_ON" not in rep.by_regime[5]

    def test_a_rescore_of_the_same_judgement_counts_once(self, world: World) -> None:
        signal(world, world.ids[0], day=0, action=SignalAction.BUY_INTEREST)
        signal(world, world.ids[0], day=0, action=SignalAction.BUY_INTEREST)
        forward_service.evaluate_signals(world.session, now=NOW)

        rep = forward_service.report(world.session, strategy_version=VERSION)
        assert rep.by_action[5]["BUY_INTEREST"].n == 1

    def test_a_signal_written_after_its_entry_is_not_forward(self, world: World) -> None:
        signal(world, world.ids[0], day=0, action=SignalAction.BUY_INTEREST, late=True)
        forward_service.evaluate_signals(world.session, now=NOW)
        rep = forward_service.report(world.session, strategy_version=VERSION)
        assert 5 not in rep.by_action

    def test_strategies_are_reported_apart(self, world: World) -> None:
        """Another rule's judgements are not this rule's record."""
        signal(world, world.ids[0], day=0, action=SignalAction.BUY_INTEREST, version="other")
        forward_service.evaluate_signals(world.session, now=NOW)
        rep = forward_service.report(world.session, strategy_version=VERSION)
        assert 5 not in rep.by_action


class TestCandidates:
    def test_a_listed_name_is_entered_at_the_next_open(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asof = KR.session_close(SESSIONS[0]) + timedelta(hours=2)
        found = Discovery(
            asof=asof,
            window=timedelta(hours=24),
            baseline=timedelta(days=14),
            coverage_start=None,
            newest_article=None,
            freshness=Freshness.FRESH,
            considered=2,
            unmeasured=0,
            candidates=[
                Candidate(
                    instrument_id=world.ids[1],
                    name=NAMES[1],
                    symbol="990981",
                    listing=None,
                    recent=9,
                    baseline=1,
                    recent_days=1.0,
                    baseline_days=5.0,
                    expected=0.2,
                    score=8.3,
                )
            ],
        )
        monkeypatch.setattr(discovery_service, "discover", lambda *a, **k: found)
        asked: list[list[int]] = []

        def fetch(session: Session, ids: Any) -> CollectorStatus:
            asked.append(list(ids))
            return CollectorStatus.SUCCESS

        assert forward_service.snapshot_candidates(world.session, now=asof) == 1
        added = forward_service.evaluate_candidates(world.session, now=NOW, fetch=fetch)

        assert added >= 3
        assert len(asked) == 1 and world.ids[1] in asked[0]
        snap = world.session.execute(
            select(CandidateSnapshot).where(CandidateSnapshot.instrument_id == world.ids[1])
        ).scalar_one()
        one = world.session.execute(
            select(CandidateOutcome).where(
                CandidateOutcome.snapshot_id == snap.id, CandidateOutcome.horizon_sessions == 1
            )
        ).scalar_one()
        assert one.entry_at == KR.session_open(SESSIONS[1])
        assert (snap.rank, snap.score) == (1, pytest.approx(8.3))

    def test_the_same_name_listed_twice_for_one_open_counts_once(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-run list the same evening as the scheduled one is the same bet."""
        asked: list[tuple[str, datetime]] = []

        def regime_at(_: Session, code: str, asof: datetime) -> Regime:
            asked.append((code, asof))
            return Regime(Label.RISK_ON)

        monkeypatch.setattr(regime_service, "regime_at", regime_at)
        first = KR.session_close(SESSIONS[0]) + timedelta(hours=1)
        for asof in (first, first + timedelta(minutes=20)):
            monkeypatch.setattr(
                discovery_service, "discover", lambda *a, _asof=asof, **k: listing(world, _asof)
            )
            forward_service.snapshot_candidates(world.session, now=asof)
        forward_service.evaluate_candidates(
            world.session, now=NOW, fetch=lambda *_: CollectorStatus.SUCCESS
        )

        rep = forward_service.report(
            world.session, strategy_version=VERSION, instrument_ids=world.ids
        )
        ours = (
            world.session.execute(
                select(CandidateSnapshot).where(CandidateSnapshot.instrument_id == world.ids[1])
            )
            .scalars()
            .all()
        )
        assert len(ours) == 2
        assert (rep.candidates[5]["top 5"].n, rep.candidates[5]["top 5"].days) == (1, 1)
        # Read at the listing's own moment, against the KOSPI for a name of no known board.
        assert rep.candidates_by_regime[5]["RISK_ON"].n == 1
        assert asked and all(code == "^KS11" for code, _ in asked)
        assert {asof for _, asof in asked} <= {s.asof for s in ours}


def listing(world: World, asof: datetime) -> Discovery:
    return Discovery(
        asof=asof,
        window=timedelta(hours=24),
        baseline=timedelta(days=14),
        coverage_start=None,
        newest_article=None,
        freshness=Freshness.FRESH,
        considered=1,
        unmeasured=0,
        candidates=[
            Candidate(
                instrument_id=world.ids[1],
                name=NAMES[1],
                symbol="990981",
                listing=None,
                recent=9,
                baseline=1,
                recent_days=1.0,
                baseline_days=5.0,
                expected=0.2,
                score=8.3,
            )
        ],
    )
