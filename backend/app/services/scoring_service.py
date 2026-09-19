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
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.core.types import Engine, MissingFactorPolicy, ScoredSignal
from app.engines.technical import PriceSeries, TechnicalEngine, TechnicalParams
from app.models import Instrument, Interval, Signal, SignalFactor
from app.repositories import candle_repo, instrument_repo
from app.scoring.availability import SessionFreshnessRule, evaluate_freshness
from app.scoring.combine import Thresholds, build_signal

logger = logging.getLogger(__name__)

STRATEGY_VERSION = "v0.1-technical"

# Phase 1 runs the technical factor alone, so it carries the full weight.
# Fundamental, sentiment and portfolio join in later phases and the weights
# move into strategy_config at that point.
WEIGHTS: dict[Engine, float] = {Engine.TECHNICAL: 1.0}
REQUIRED: frozenset[Engine] = frozenset({Engine.TECHNICAL})

HISTORY_BARS = 250


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

    bars = candle_repo.history(
        session, instrument.instrument_id, Interval.DAY_1, limit=HISTORY_BARS
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
    factor, reasons = engine.evaluate(
        series,
        requested_weight=WEIGHTS[Engine.TECHNICAL],
        provenance=provenance,
    )

    # `data_asof` is when the inputs became knowable, not when the last bar
    # opened. A daily bar carries a close that does not exist until the session
    # ends, so using `ts` would claim the score was computed from data nobody
    # had yet. Deriving both from the bar rather than from "now" keeps live
    # scoring and backtesting identical.
    data_asof = bars[-1].available_at
    decision_at = data_asof

    return build_signal(
        instrument_id=instrument.instrument_id,
        factors=(factor,),
        reasons=reasons,
        data_asof=data_asof,
        decision_at=decision_at,
        calendar=calendar,
        strategy_version=STRATEGY_VERSION,
        policy=MissingFactorPolicy.ABSTAIN,
        required_factors=REQUIRED,
        thresholds=Thresholds(),
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
