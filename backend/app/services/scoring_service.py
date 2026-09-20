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
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.base import CollectorStatusLookup
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.core.types import (
    Availability,
    Engine,
    Factor,
    MissingFactorPolicy,
    ScoredSignal,
    SignalReason,
)
from app.engines.fundamental import FundamentalEngine
from app.engines.technical import PriceSeries, TechnicalEngine, TechnicalParams
from app.models import Instrument, Interval, Signal, SignalFactor
from app.models.fundamental import FundamentalSource
from app.repositories import candle_repo, fundamental_repo, instrument_repo
from app.scoring.availability import (
    SessionFreshnessRule,
    SourceCheckFreshnessRule,
    evaluate_freshness,
    resolve_availability,
)
from app.scoring.combine import ExecutionTimingError, Thresholds, build_signal
from app.services import fundamental_service

logger = logging.getLogger(__name__)

STRATEGY_VERSION = "v0.2-technical-fundamental"

# The base judgement layer. Sentiment is deliberately absent: it is an
# event overlay with a different half-life, not a weighted factor, so it
# never enters this sum. Portfolio joins in Phase 5. Weights move into
# strategy_config once there is more than one strategy to version.
WEIGHTS: dict[Engine, float] = {
    Engine.TECHNICAL: 0.6,
    Engine.FUNDAMENTAL: 0.4,
}

# Only technical is required. Fundamentals are genuinely unavailable for
# instruments SEC does not cover — every Korean listing, until DART is
# wired — and abstaining on all of them would make the system useless
# exactly where it is most needed.
REQUIRED: frozenset[Engine] = frozenset({Engine.TECHNICAL})

# How stale a fundamental source check may be before the factor sits out.
# Judged on when the source was last reached, not on the age of the filing:
# a quarterly report is old by nature, and the risk being guarded against is
# missing a *new* one.
FUNDAMENTAL_SOURCE_CHECK = timedelta(days=7)

HISTORY_BARS = 250

# Which value source speaks for which market. Korean instruments have no
# SEC coverage at all, so their fundamental factor sits out until the DART
# collector lands — and says so rather than scoring zero.
_SOURCE_FOR: dict[Market, FundamentalSource] = {
    Market.US: FundamentalSource.SEC,
    Market.KR: FundamentalSource.DART,
}
_CURRENCY: dict[Market, str] = {Market.US: "USD", Market.KR: "KRW"}


def score_instrument(
    session: Session,
    instrument: Instrument,
    *,
    now: datetime | None = None,
    params: TechnicalParams | None = None,
) -> ScoredSignal | None:
    """Score one instrument from its stored history.

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
        limit=HISTORY_BARS,
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
    )

    policy = MissingFactorPolicy.ABSTAIN
    factors = tuple(
        _apply_freshness(f, policy=policy, required=f.engine in REQUIRED)
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
        thresholds=Thresholds(),
    )


def _apply_freshness(factor: Factor, *, policy: MissingFactorPolicy, required: bool) -> Factor:
    """Let the freshness verdict actually reduce the factor's weight.

    Engines report what they could compute; they do not judge whether the data
    behind it is current enough to use. That decision belongs here, where the
    strategy's policy lives.

    Without this step the whole freshness chain was computed and then ignored —
    a factor could be marked STALE and still contribute at full weight, which
    made the provenance shown in the UI a decoration rather than a control.
    """
    if factor.availability is Availability.UNAVAILABLE:
        return factor

    verdict = resolve_availability(
        factor.engine,
        provenance=factor.provenance,
        requested_weight=factor.requested_weight,
        policy=policy,
        is_required=required,
    )
    if verdict.availability is Availability.AVAILABLE:
        return factor

    return replace(
        factor,
        availability=verdict.availability,
        effective_weight=verdict.effective_weight,
        availability_reason=verdict.reason,
    )


def _score_fundamental(
    session: Session,
    instrument: Instrument,
    *,
    asof: datetime,
    price: float,
    now: datetime,
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
    instruments = instrument_repo.list_active(session, asof=now.date())

    persisted: list[Signal] = []
    for instrument in instruments:
        signal = score_instrument(session, instrument, now=now)
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
