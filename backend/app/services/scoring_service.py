"""Scoring orchestration.

The seam between persistence and the pure engines. This module is allowed to
query; the engines it calls are not. It loads data through repositories, hands
plain values to the engines, and writes the result back.

Keeping that boundary sharp is what lets the same engine code run in the live
path and inside a backtest: the engine cannot tell which it is in, because it
only ever sees a `PriceSeries` and an `asof`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.base import CollectorStatusLookup
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.core.types import (
    Engine,
    Factor,
    ScoredSignal,
    SignalReason,
)
from app.engines.fundamental import FundamentalEngine, PeerRatios
from app.engines.technical import PriceSeries, TechnicalEngine, TechnicalParams
from app.models import Instrument, Interval, Signal, SignalFactor
from app.models.fundamental import FundamentalSource
from app.repositories import candle_repo, fundamental_repo, instrument_repo
from app.scoring.availability import (
    SessionFreshnessRule,
    SourceCheckFreshnessRule,
    evaluate_freshness,
)
from app.scoring.combine import ExecutionTimingError, build_signal
from app.scoring.policy import (
    POLICY,
    REQUIRED,
    SCORING_HISTORY_BARS,
    STRATEGY_VERSION,
    THRESHOLDS,
    WEIGHTS,
    apply_freshness,
)
from app.services import fundamental_service

logger = logging.getLogger(__name__)

# How stale a fundamental source check may be before the factor sits out.
# Judged on when the source was last reached, not on the age of the filing:
# a quarterly report is old by nature, and the risk being guarded against is
# missing a *new* one.
FUNDAMENTAL_SOURCE_CHECK = timedelta(days=7)


# Which value source speaks for which market. Korean instruments have no
# SEC coverage at all, so their fundamental factor sits out until the DART
# collector lands — and says so rather than scoring zero.
_SOURCE_FOR: dict[Market, FundamentalSource] = {
    Market.US: FundamentalSource.SEC,
    Market.KR: FundamentalSource.DART,
}
_CURRENCY: dict[Market, str] = {Market.US: "USD", Market.KR: "KRW"}


# Resolves the peer population for one market at one instant. Passed in rather
# than built here so that scoring a single instrument stays possible without a
# universe: the lookup returns None, every ratio falls back to its fixed scale,
# and each metric says so.
PeerLookup = Callable[[Market, datetime], PeerRatios | None]


def peer_ratios(
    session: Session,
    instruments: list[Instrument],
    *,
    asof: datetime,
    source: FundamentalSource,
    currency: str,
) -> PeerRatios:
    """The market's cross-sectional population, at one instant.

    Built with `price=None` on purpose. The ranked ratios are ROE, debt ratio,
    operating margin and revenue growth, none of which touch the price, and
    valuation is scored on its fixed scale precisely because a rank over raw
    multiples is not a belief this strategy holds. Fetching nine last closes to
    feed a ratio nobody ranks would be work done to no end.

    Every peer is resolved at the same instant as the instrument being scored,
    so a rank compares what was knowable then rather than mixing eras.
    """
    snapshots = [
        fundamental_service.build_snapshot(
            session,
            instrument.instrument_id,
            asof=asof,
            price=None,
            currency=currency,
            source=source,
        )
        for instrument in instruments
    ]
    return PeerRatios.of(asof, snapshots)


def market_peer_lookup(session: Session, instruments: list[Instrument]) -> PeerLookup:
    """A lookup over `instruments`, memoised per market and instant.

    Instruments in one market almost always share a session close, so the
    cache normally collapses a population per instrument into one per market.
    It is keyed on the instant as well, because two instruments whose newest
    bar differs must not silently share a population built at the later one.
    """
    by_market: dict[Market, list[Instrument]] = defaultdict(list)
    for instrument in instruments:
        by_market[instrument.market].append(instrument)

    cache: dict[tuple[Market, datetime], PeerRatios] = {}

    def lookup(market: Market, asof: datetime) -> PeerRatios | None:
        members = by_market.get(market)
        if not members:
            return None
        key = (market, asof)
        if key not in cache:
            cache[key] = peer_ratios(
                session,
                members,
                asof=asof,
                source=_SOURCE_FOR[market],
                currency=_CURRENCY[market],
            )
        return cache[key]

    return lookup


def score_instrument(
    session: Session,
    instrument: Instrument,
    *,
    now: datetime | None = None,
    params: TechnicalParams | None = None,
    peers: PeerLookup | None,
) -> ScoredSignal | None:
    """Score one instrument from its stored history.

    `peers` has no default. It decides whether the fundamental ratios are
    ranked against the market or mapped onto their fixed scale, which is a
    difference in the rule rather than in the plumbing - and the signal is
    stamped `STRATEGY_VERSION` either way, so a caller that silently inherited
    None would persist rows labelled cross-sectional that were never ranked.
    Passing None is allowed and is what scoring outside a universe means; it
    just has to be said rather than fallen into.

    Returns None when there are no bars at all — distinct from abstaining,
    which is a judgement about a known-empty factor rather than an absence of
    any data to judge.
    """
    now = now or utc_now()
    calendar = MarketCalendar(instrument.market)

    # `available_before=now` is not optional. Without it a bar that has opened
    # but not closed comes back, and the scorer reads an OHLCV that is still
    # being formed — producing a signal stamped with a data_asof in the future.
    # The backtest was already going to apply this filter; the live path has to
    # apply the same one, or the two stop being comparable.
    bars = candle_repo.history(
        session,
        instrument.instrument_id,
        Interval.DAY_1,
        limit=SCORING_HISTORY_BARS,
        available_before=now,
    )
    if not bars:
        logger.info("instrument %s: no bars stored", instrument.instrument_id)
        return None

    series = PriceSeries(
        instrument_id=instrument.instrument_id,
        closes=tuple(float(b.close) for b in bars),
        volumes=tuple(float(b.volume) for b in bars),
        asof=bars[-1].available_at,
    )

    # Technical freshness counts trading sessions, not calendar days, so that
    # Friday's close is not called stale on Monday morning.
    provenance = evaluate_freshness(
        SessionFreshnessRule(),
        now=now,
        source_asof=bars[-1].available_at,
        calendar=calendar,
    )

    engine = TechnicalEngine(params)
    technical, technical_reasons = engine.evaluate(
        series,
        requested_weight=WEIGHTS[Engine.TECHNICAL],
        provenance=provenance,
    )

    fundamental, fundamental_reasons = _score_fundamental(
        session,
        instrument,
        asof=bars[-1].available_at,
        price=float(bars[-1].close),
        now=now,
        peers=peers,
    )

    policy = POLICY
    factors = tuple(
        apply_freshness(f, policy=policy, required=f.engine in REQUIRED)
        for f in (technical, fundamental)
    )
    # Evidence from a factor that has just been stood down would claim more
    # than the score does.
    reasons = tuple(
        r
        for r in technical_reasons + fundamental_reasons
        if r.engine not in {f.engine for f in factors if f.effective_weight == 0.0}
    )

    # `data_asof` is when the inputs became knowable, not when the last bar
    # opened. A daily bar carries a close that does not exist until the session
    # ends, so using `ts` would claim the score was computed from data nobody
    # had yet. Deriving both from the bar rather than from "now" keeps live
    # scoring and backtesting identical.
    data_asof = bars[-1].available_at
    decision_at = data_asof

    # Defence in depth. If this ever fires, the availability filter above has
    # stopped working — an internal invariant, so it raises rather than being
    # quietly clamped to `now`.
    if data_asof > now:
        raise ExecutionTimingError(
            f"data_asof {data_asof.isoformat()} is in the future relative to "
            f"{now.isoformat()}: an incomplete bar reached the scorer"
        )

    return build_signal(
        instrument_id=instrument.instrument_id,
        factors=factors,
        reasons=reasons,
        data_asof=data_asof,
        decision_at=decision_at,
        calendar=calendar,
        strategy_version=STRATEGY_VERSION,
        policy=policy,
        required_factors=REQUIRED,
        thresholds=THRESHOLDS,
    )


def _score_fundamental(
    session: Session,
    instrument: Instrument,
    *,
    asof: datetime,
    price: float,
    now: datetime,
    peers: PeerLookup | None,
) -> tuple[Factor, tuple[SignalReason, ...]]:
    """Score reported financials as of the same instant as the price data.

    Freshness is judged on when the source was last successfully checked, not
    on how old the newest filing is. A company between quarters has nothing
    newer to report, and calling that stale would drop the factor for three
    months at a time. What matters is whether we would have noticed a new
    filing.
    """
    checked_at = CollectorStatusLookup(session).last_success(_SOURCE_FOR[instrument.market])
    newest_filing = fundamental_repo.latest_filing_date(session, instrument.instrument_id)

    provenance = evaluate_freshness(
        SourceCheckFreshnessRule(max_check_age=FUNDAMENTAL_SOURCE_CHECK),
        now=now,
        source_asof=(
            datetime.combine(newest_filing, datetime.min.time(), tzinfo=UTC)
            if newest_filing
            else None
        ),
        source_checked_at=checked_at,
    )

    snapshot = fundamental_service.build_snapshot(
        session,
        instrument.instrument_id,
        asof=asof,
        price=price,
        currency=_CURRENCY[instrument.market],
        source=_SOURCE_FOR[instrument.market],
    )

    return FundamentalEngine().evaluate(
        snapshot,
        requested_weight=WEIGHTS[Engine.FUNDAMENTAL],
        provenance=provenance,
        peers=peers(instrument.market, asof) if peers is not None else None,
    )


def persist_signal(session: Session, signal: ScoredSignal) -> Signal:
    """Write a signal and its factor decomposition.

    Every metric's raw value, normalized position, both weights and the
    resulting contribution are stored, so the question "why was this 59.7?"
    is answerable from rows alone without recomputing anything.
    """
    row = Signal(
        instrument_id=signal.instrument_id,
        data_asof=signal.data_asof,
        decision_at=signal.decision_at,
        earliest_execution_at=signal.earliest_execution_at,
        total_score=signal.total_score,
        action=signal.action,
        policy=signal.policy,
        abstained_reason=signal.abstained_reason,
        reasons=[
            {
                "status": r.status.value,
                "text": r.text,
                "engine": r.engine.value,
                "metric_name": r.metric_name or "",
            }
            for r in signal.reasons
        ],
        strategy_version=signal.strategy_version,
    )
    session.add(row)
    session.flush()

    for factor in signal.factors:
        session.add(
            SignalFactor(
                signal_id=row.id,
                engine=factor.engine,
                score=factor.score,
                metrics=[
                    {
                        "name": m.name,
                        "raw": m.raw,
                        "normalized": m.normalized,
                        "detail": m.detail,
                    }
                    for m in factor.metrics
                ],
                requested_weight=factor.requested_weight,
                effective_weight=factor.effective_weight,
                contribution=factor.contribution,
                availability=factor.availability,
                availability_reason=factor.availability_reason,
                source_asof=factor.provenance.source_asof,
                source_checked_at=factor.provenance.source_checked_at,
                data_age=factor.provenance.data_age,
                freshness_status=factor.provenance.freshness,
            )
        )

    session.commit()
    return row


def score_all(session: Session, *, now: datetime | None = None) -> list[Signal]:
    """Score every active instrument and persist the results."""
    now = now or utc_now()
    # Tracked only, and for two separate reasons. Scoring a name we hold no
    # prices or filings for is not possible; scoring one whose prices we happen
    # to have but whose coverage we never established would persist a signal
    # for an instrument nobody chose to follow. A listing master makes both
    # reachable, since it adds thousands of rows that are names and nothing else.
    instruments = instrument_repo.list_active(session, asof=now.date(), tracked=True)

    # The tracked universe, grouped by market, so each instrument is
    # ranked against the peers it actually has. Scoring one instrument alone
    # cannot build this, which is why the lookup is assembled here and passed
    # down rather than being reached for inside the scorer.
    peers = market_peer_lookup(session, instruments)

    persisted: list[Signal] = []
    for instrument in instruments:
        signal = score_instrument(session, instrument, now=now, peers=peers)
        if signal is None:
            continue
        persisted.append(persist_signal(session, signal))
        logger.info(
            "scored %s: %s %.1f",
            instrument.name,
            signal.action.value,
            signal.total_score,
        )
    return persisted


def latest_signal(session: Session, instrument_id: int) -> Signal | None:
    """Most recent signal for one instrument."""
    stmt = (
        select(Signal)
        .where(Signal.instrument_id == instrument_id)
        .order_by(Signal.decision_at.desc(), Signal.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def resolve(session: Session, symbol: str, market: Market) -> Instrument | None:
    return instrument_repo.resolve_symbol(session, symbol, market, asof=utc_now().date())
