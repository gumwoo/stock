"""The forward test: recording what the system said, then what happened.

Three jobs, run after each Korean close by the worker and by hand with the
`forward` commands:

- `evaluate_signals` adds returns to signals whose horizon has closed. Entry
  is the signal's `earliest_execution_at` open — the backtest's rule — and the
  exit is the close of the `h`-th session counting the entry session as the
  first. ABSTAINED signals said nothing to measure and are skipped.
- `snapshot_candidates` stores the day's discovery list as it stood.
- `evaluate_candidates` does for listed names what the first does for
  signals, entering at the first open after the list was taken. Most listed
  names are untracked and have no prices; they are fetched for the purpose,
  under a run name of their own, without promoting anything.

`report` is where the record is read. It is deliberately plain — counts,
means, hit rates and returns in excess of the same day's cross-section — and
states the number of observations next to every figure. Weeks of this are a
handful of independent days; nothing in it is significant until months are.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.collectors.base import run_collector
from app.collectors.yfinance_history import YFinanceHistoryCollector
from app.core.calendar import Market, MarketCalendar
from app.core.clock import ensure_utc, utc_now
from app.core.types import SignalAction
from app.models import (
    CandidateOutcome,
    CandidateSnapshot,
    Instrument,
    Interval,
    Signal,
    SignalOutcome,
    SignalOverlay,
    SignalRegime,
)
from app.models.collector import CollectorStatus
from app.models.forward import HORIZONS
from app.repositories import candle_repo
from app.scoring.policy import STRATEGY_VERSION
from app.services import discovery_service, regime_service

# How far back evaluation looks for rows still missing an outcome. The longest
# horizon is twenty sessions, about a month; twice that leaves room for gaps.
LOOKBACK = timedelta(days=60)
# Recorded candidates per day.
SNAPSHOT_TOP = 20
FORWARD_RUN_PREFIX = "FORWARD_"
# Overlay points beyond which a signal counts as carrying good or bad news.
OVERLAY_BAND = 1.0

Fetch = Callable[[Session, Sequence[int]], CollectorStatus]


def fetch_candidate_prices(session: Session, instrument_ids: Sequence[int]) -> CollectorStatus:
    """Daily bars for listed candidates, recorded under a forward-test run name."""
    collector = YFinanceHistoryCollector(period="3mo", instrument_ids=instrument_ids)
    collector.name = FORWARD_RUN_PREFIX + collector.name
    return run_collector(collector, session).status


@dataclass(frozen=True, slots=True)
class Measured:
    horizon: int
    entry_price: float
    exit_at: datetime
    exit_price: float

    @property
    def return_pct(self) -> float:
        return (self.exit_price / self.entry_price - 1.0) * 100.0


def measure(
    session: Session,
    *,
    instrument_id: int,
    market: Market,
    entry_at: datetime,
    now: datetime,
) -> list[Measured]:
    """Returns over each horizon whose exit session has closed and whose bars exist."""
    calendar = MarketCalendar(market)
    entry_at = ensure_utc(entry_at, field="entry_at")
    entry = candle_repo.opening_price(session, instrument_id, Interval.DAY_1, entry_at)
    if entry is None or entry <= 0:
        return []
    entry_day = entry_at.date()
    end = min(entry_day + timedelta(days=max(HORIZONS) * 2 + 15), calendar.last_session)
    if end < entry_day:
        return []
    sessions = calendar.sessions_between(entry_day, end)
    out: list[Measured] = []
    for horizon in HORIZONS:
        if len(sessions) < horizon:
            continue
        day = sessions[horizon - 1]
        closes_at = calendar.session_close(day)
        if closes_at > now:
            continue
        opens_at = calendar.session_open(day)
        bars = candle_repo.history(
            session, instrument_id, Interval.DAY_1, since=opens_at, until=opens_at, limit=1
        )
        if not bars or bars[-1].ts != opens_at:
            continue
        out.append(
            Measured(
                horizon=horizon,
                entry_price=float(entry),
                exit_at=closes_at,
                exit_price=float(bars[-1].close),
            )
        )
    return out


def _open_signals(session: Session, now: datetime) -> list[tuple[Signal, Market]]:
    measured = (
        select(SignalOutcome.signal_id, func.count().label("n"))
        .group_by(SignalOutcome.signal_id)
        .subquery()
    )
    stmt = (
        select(Signal, Instrument.market)
        .join(Instrument, Instrument.instrument_id == Signal.instrument_id)
        .outerjoin(measured, measured.c.signal_id == Signal.id)
        .where(
            Signal.action != SignalAction.ABSTAINED,
            Signal.earliest_execution_at <= now,
            Signal.earliest_execution_at > now - LOOKBACK,
            func.coalesce(measured.c.n, 0) < len(HORIZONS),
        )
    )
    return [(signal, market) for signal, market in session.execute(stmt).all()]


def evaluate_signals(session: Session, *, now: datetime | None = None) -> int:
    """Add the outcomes that have become measurable. Commits. Returns rows added."""
    now = ensure_utc(now, field="now") if now is not None else utc_now()
    added = 0
    for signal, market in _open_signals(session, now):
        rows = [
            {
                "signal_id": signal.id,
                "horizon_sessions": m.horizon,
                "entry_at": signal.earliest_execution_at,
                "entry_price": m.entry_price,
                "exit_at": m.exit_at,
                "exit_price": m.exit_price,
                "return_pct": m.return_pct,
            }
            for m in measure(
                session,
                instrument_id=signal.instrument_id,
                market=market,
                entry_at=signal.earliest_execution_at,
                now=now,
            )
        ]
        if rows:
            stmt = (
                pg_insert(SignalOutcome)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_signal_outcome_horizon")
            )
            added += len(session.execute(stmt.returning(SignalOutcome.id)).scalars().all())
    session.commit()
    return added


def snapshot_candidates(
    session: Session, *, now: datetime | None = None, top: int = SNAPSHOT_TOP
) -> int:
    """Store today's discovery list as it stands. Commits. Returns names stored."""
    found = discovery_service.discover(session, asof=now, top=top)
    if not found.candidates:
        return 0
    rows = [
        {
            "asof": found.asof,
            "rank": n,
            "instrument_id": c.instrument_id,
            "recent_mentions": c.recent,
            "baseline_mentions": c.baseline,
            "recent_days": c.recent_days,
            "baseline_days": c.baseline_days,
            "score": c.score,
            "news_freshness": found.freshness.value,
        }
        for n, c in enumerate(found.candidates, 1)
    ]
    stmt = (
        pg_insert(CandidateSnapshot)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_candidate_snapshot_asof_instrument")
    )
    stored = len(session.execute(stmt.returning(CandidateSnapshot.id)).scalars().all())
    session.commit()
    return stored


def evaluate_candidates(
    session: Session, *, now: datetime | None = None, fetch: Fetch = fetch_candidate_prices
) -> int:
    """Add outcomes for listed candidates, fetching their prices first. Commits."""
    now = ensure_utc(now, field="now") if now is not None else utc_now()
    calendar = MarketCalendar(Market.KR)
    measured = (
        select(CandidateOutcome.snapshot_id, func.count().label("n"))
        .group_by(CandidateOutcome.snapshot_id)
        .subquery()
    )
    open_rows = (
        session.execute(
            select(CandidateSnapshot)
            .outerjoin(measured, measured.c.snapshot_id == CandidateSnapshot.id)
            .where(
                CandidateSnapshot.asof > now - LOOKBACK,
                func.coalesce(measured.c.n, 0) < len(HORIZONS),
            )
        )
        .scalars()
        .all()
    )
    due = [
        (row, calendar.next_tradable_open(row.asof))
        for row in open_rows
        if calendar.next_tradable_open(row.asof) <= now
    ]
    if not due:
        return 0
    fetch(session, sorted({row.instrument_id for row, _ in due}))

    added = 0
    for row, entry_at in due:
        results = measure(
            session,
            instrument_id=row.instrument_id,
            market=Market.KR,
            entry_at=entry_at,
            now=now,
        )
        if not results:
            continue
        stmt = (
            pg_insert(CandidateOutcome)
            .values(
                [
                    {
                        "snapshot_id": row.id,
                        "horizon_sessions": m.horizon,
                        "entry_at": entry_at,
                        "entry_price": m.entry_price,
                        "exit_at": m.exit_at,
                        "exit_price": m.exit_price,
                        "return_pct": m.return_pct,
                    }
                    for m in results
                ]
            )
            .on_conflict_do_nothing(constraint="uq_candidate_outcome_horizon")
        )
        added += len(session.execute(stmt.returning(CandidateOutcome.id)).scalars().all())
    session.commit()
    return added


# --- reading the record ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stats:
    n: int
    days: int
    mean: float | None
    median: float | None
    hit_rate: float | None
    mean_excess: float | None


def _stats(returns: Sequence[float], excess: Sequence[float], days: int) -> Stats:
    if not returns:
        return Stats(0, days, None, None, None, None)
    return Stats(
        n=len(returns),
        days=days,
        mean=statistics.fmean(returns),
        median=statistics.median(returns),
        hit_rate=sum(1 for r in returns if r > 0) / len(returns),
        mean_excess=statistics.fmean(excess) if excess else None,
    )


@dataclass
class ForwardReport:
    by_action: dict[int, dict[str, Stats]] = field(default_factory=dict)
    by_overlay: dict[int, dict[str, Stats]] = field(default_factory=dict)
    by_regime: dict[int, dict[str, Stats]] = field(default_factory=dict)
    candidates: dict[int, dict[str, Stats]] = field(default_factory=dict)
    candidates_by_regime: dict[int, dict[str, Stats]] = field(default_factory=dict)
    signals_recorded: int = 0
    snapshots_recorded: int = 0


def _first_signals(session: Session, strategy_version: str) -> list[int]:
    """One signal per instrument and decision moment under one strategy: the first made.

    A manual rescore on top of the scheduled one would otherwise count the
    same judgement twice, and two strategy versions are two different rules.
    A signal written after its own entry time is left out: it was made with
    the entry already past, which is a backtest of one day, not a forward test.
    """
    first = (
        select(func.min(Signal.id))
        .where(
            Signal.strategy_version == strategy_version,
            Signal.ingested_at <= Signal.earliest_execution_at,
        )
        .group_by(Signal.instrument_id, Signal.decision_at)
    )
    return list(session.execute(first).scalars())


def report(
    session: Session,
    *,
    strategy_version: str = STRATEGY_VERSION,
    instrument_ids: Collection[int] | None = None,
) -> ForwardReport:
    """The record so far. `instrument_ids` narrows it — and with it the
    cross-section that excess returns are measured against."""
    out = ForwardReport()
    ids = _first_signals(session, strategy_version)
    out.signals_recorded = len(ids)
    rows = session.execute(
        select(
            SignalOutcome.horizon_sessions,
            SignalOutcome.return_pct,
            SignalOutcome.entry_at,
            Signal.action,
            Instrument.market,
            SignalOverlay.points,
            SignalRegime.label,
        )
        .join(Signal, Signal.id == SignalOutcome.signal_id)
        .join(Instrument, Instrument.instrument_id == Signal.instrument_id)
        .outerjoin(SignalOverlay, SignalOverlay.signal_id == Signal.id)
        .outerjoin(
            SignalRegime,
            # One version's labels: a changed threshold is a different grouping.
            and_(
                SignalRegime.signal_id == Signal.id,
                SignalRegime.regime_version == regime_service.PARAMS.version,
            ),
        )
        .where(
            Signal.id.in_(ids),
            *(
                [Signal.instrument_id.in_(list(instrument_ids))]
                if instrument_ids is not None
                else []
            ),
        )
    ).all()

    # The same day's cross-section, per market and horizon: what an equal
    # weight in every judged name would have done.
    pool: dict[tuple[int, str, datetime], list[float]] = defaultdict(list)
    for h, r, entry_at, _, market, _, _ in rows:
        pool[(h, market.value, entry_at)].append(r)
    base = {key: statistics.fmean(v) for key, v in pool.items()}

    def bucket(points: float | None) -> str:
        if points is None:
            return "no overlay"
        if points >= OVERLAY_BAND:
            return "good news"
        if points <= -OVERLAY_BAND:
            return "bad news"
        return "quiet"

    groups: dict[tuple[str, int, str], list[tuple[float, float, datetime]]] = defaultdict(list)
    for h, r, entry_at, action, market, points, regime in rows:
        excess = r - base[(h, market.value, entry_at)]
        groups[("action", h, action.value)].append((r, excess, entry_at))
        groups[("overlay", h, bucket(points))].append((r, excess, entry_at))
        groups[("regime", h, regime or "no regime")].append((r, excess, entry_at))
    tables = {"action": out.by_action, "overlay": out.by_overlay, "regime": out.by_regime}
    for (kind, h, label), items in groups.items():
        target = tables[kind]
        target.setdefault(h, {})[label] = _stats(
            [i[0] for i in items], [i[1] for i in items], len({i[2] for i in items})
        )

    # One listing per name and entry: a hand-run list taken the same evening
    # as the scheduled one enters at the same open, and is the same bet.
    first_listing = (
        select(func.min(CandidateOutcome.snapshot_id))
        .join(CandidateSnapshot, CandidateSnapshot.id == CandidateOutcome.snapshot_id)
        .group_by(CandidateSnapshot.instrument_id, CandidateOutcome.entry_at)
    )
    snaps = session.execute(
        select(
            CandidateOutcome.horizon_sessions,
            CandidateOutcome.return_pct,
            CandidateOutcome.entry_at,
            CandidateSnapshot.rank,
            CandidateSnapshot.asof,
            Instrument.listing,
        )
        .join(CandidateSnapshot, CandidateSnapshot.id == CandidateOutcome.snapshot_id)
        .join(Instrument, Instrument.instrument_id == CandidateSnapshot.instrument_id)
        .where(
            CandidateOutcome.snapshot_id.in_(first_listing),
            *(
                [CandidateSnapshot.instrument_id.in_(list(instrument_ids))]
                if instrument_ids is not None
                else []
            ),
        )
    ).all()
    out.snapshots_recorded = int(
        session.execute(select(func.count()).select_from(CandidateSnapshot)).scalar_one()
    )
    kr = {key: value for key, value in base.items() if key[1] == Market.KR.value}
    cand: dict[tuple[str, int, str], list[tuple[float, float | None, datetime]]] = defaultdict(list)
    # A candidate list carries no stored regime; the index closes it would be
    # read from are the same at any later reading, so it is read here.
    regimes: dict[tuple[str, datetime], str] = {}
    for h, r, entry_at, rank, asof, listing in snaps:
        reference = kr.get((h, Market.KR.value, entry_at))
        pick = (r, None if reference is None else r - reference, entry_at)
        cand[("rank", h, "top 5" if rank <= 5 else "6-20")].append(pick)
        code = regime_service.index_for(Market.KR, listing)
        if (code, asof) not in regimes:
            regimes[(code, asof)] = regime_service.regime_at(session, code, asof).label
        cand[("regime", h, regimes[(code, asof)])].append(pick)
    for (kind, h, label), picks in cand.items():
        target = out.candidates if kind == "rank" else out.candidates_by_regime
        target.setdefault(h, {})[label] = _stats(
            [p[0] for p in picks],
            [p[1] for p in picks if p[1] is not None],
            len({p[2] for p in picks}),
        )
    return out
