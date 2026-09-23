"""Signal and market-data endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import (
    CandleOut,
    FactorOut,
    InstrumentOut,
    MetricOut,
    OverlayEventOut,
    OverlayOut,
    ReasonOut,
    SignalOut,
)
from app.core.clock import utc_now
from app.db import get_db
from app.models import Instrument, Interval, Signal, SignalOverlay
from app.repositories import candle_repo, instrument_repo
from app.services import scoring_service

router = APIRouter(prefix="/api", tags=["signals"])

SessionDep = Annotated[Session, Depends(get_db)]

_CURRENCY = {"KR": "KRW", "US": "USD"}


def _symbol_of(session: Session, instrument: Instrument) -> str:
    return instrument_repo.current_symbol(session, instrument.instrument_id) or "?"


def _overlay_out(session: Session, row: Signal) -> OverlayOut | None:
    overlay = session.execute(
        select(SignalOverlay).where(SignalOverlay.signal_id == row.id)
    ).scalar_one_or_none()
    if overlay is None:
        return None
    return OverlayOut(
        points=overlay.points,
        events=overlay.events,
        readings_used=overlay.readings_used,
        unread_articles=overlay.unread_articles,
        news_freshness=overlay.news_freshness.value,
        asof=overlay.asof,
        overlay_version=overlay.overlay_version,
        top_events=[OverlayEventOut.model_validate(e) for e in overlay.detail],
    )


def _to_signal_out(session: Session, row: Signal, instrument: Instrument) -> SignalOut:
    return SignalOut(
        id=row.id,
        instrument_id=row.instrument_id,
        symbol=_symbol_of(session, instrument),
        name=instrument.name,
        market=instrument.market.value,
        total_score=row.total_score,
        action=row.action.value,
        data_asof=row.data_asof,
        decision_at=row.decision_at,
        earliest_execution_at=row.earliest_execution_at,
        strategy_version=row.strategy_version,
        policy=row.policy.value,
        abstained_reason=row.abstained_reason,
        factors=[
            FactorOut(
                engine=f.engine.value,
                score=f.score,
                metrics=[
                    MetricOut(
                        name=str(m["name"]),
                        raw=float(m["raw"]),  # type: ignore[arg-type]
                        normalized=float(m["normalized"]),  # type: ignore[arg-type]
                        detail=(str(m["detail"]) if m.get("detail") else None),
                    )
                    for m in f.metrics
                ],
                requested_weight=f.requested_weight,
                effective_weight=f.effective_weight,
                contribution=f.contribution,
                availability=f.availability.value,
                availability_reason=f.availability_reason,
                source_asof=f.source_asof,
                source_checked_at=f.source_checked_at,
                freshness_status=f.freshness_status.value,
            )
            for f in row.factors
        ],
        reasons=[
            ReasonOut(
                status=str(r["status"]),
                text=str(r["text"]),
                engine=str(r["engine"]),
                metric_name=str(r["metric_name"]) or None,
            )
            for r in row.reasons
        ],
        overlay=_overlay_out(session, row),
    )


@router.get("/instruments", response_model=list[InstrumentOut])
def list_instruments(session: SessionDep) -> list[InstrumentOut]:
    """Every scoreable instrument in the point-in-time universe as of today."""
    # Tracked only. The listing master holds thousands of names with no price
    # and no score, and listing them here would bury the ones that have both.
    instruments = instrument_repo.list_active(session, asof=utc_now().date(), tracked=True)

    out: list[InstrumentOut] = []
    for instrument in instruments:
        bars = candle_repo.history(session, instrument.instrument_id, Interval.DAY_1, limit=2)
        last = bars[-1] if bars else None
        prev = bars[-2] if len(bars) > 1 else None

        change = None
        if last is not None and prev is not None and float(prev.close) != 0:
            change = (float(last.close) - float(prev.close)) / float(prev.close) * 100.0

        out.append(
            InstrumentOut(
                instrument_id=instrument.instrument_id,
                symbol=_symbol_of(session, instrument),
                name=instrument.name,
                market=instrument.market.value,
                sector=instrument.sector,
                currency=_CURRENCY.get(instrument.market.value, "KRW"),
                last_close=float(last.close) if last else None,
                last_close_date=last.ts.date() if last else None,
                change_pct=change,
            )
        )
    return out


@router.get("/signals", response_model=list[SignalOut])
def list_signals(session: SessionDep) -> list[SignalOut]:
    """The latest signal for each tracked instrument."""
    instruments = instrument_repo.list_active(session, asof=utc_now().date(), tracked=True)

    out: list[SignalOut] = []
    for instrument in instruments:
        row = scoring_service.latest_signal(session, instrument.instrument_id)
        if row is not None:
            out.append(_to_signal_out(session, row, instrument))

    out.sort(key=lambda s: s.total_score, reverse=True)
    return out


@router.get("/signals/{instrument_id}", response_model=SignalOut)
def get_signal(instrument_id: int, session: SessionDep) -> SignalOut:
    instrument = instrument_repo.get_by_id(session, instrument_id)
    if instrument is None:
        raise HTTPException(404, f"no instrument {instrument_id}")

    row = scoring_service.latest_signal(session, instrument_id)
    if row is None:
        raise HTTPException(404, f"no signal yet for instrument {instrument_id}")

    return _to_signal_out(session, row, instrument)


@router.get("/signals/{instrument_id}/history", response_model=list[SignalOut])
def signal_history(
    instrument_id: int,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
) -> list[SignalOut]:
    instrument = instrument_repo.get_by_id(session, instrument_id)
    if instrument is None:
        raise HTTPException(404, f"no instrument {instrument_id}")

    rows = (
        session.execute(
            select(Signal)
            .where(Signal.instrument_id == instrument_id)
            .order_by(Signal.decision_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [_to_signal_out(session, r, instrument) for r in rows]


@router.get("/candles/{instrument_id}", response_model=list[CandleOut])
def get_candles(
    instrument_id: int,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=2000)] = 250,
) -> list[CandleOut]:
    """Raw daily bars, oldest first.

    Raw, not adjusted: adjustments are derived from corporate actions at read
    time so the stored history never rewrites itself.
    """
    if instrument_repo.get_by_id(session, instrument_id) is None:
        raise HTTPException(404, f"no instrument {instrument_id}")

    bars = candle_repo.history(session, instrument_id, Interval.DAY_1, limit=limit)
    return [
        CandleOut(
            ts=b.ts,
            open=float(b.open),
            high=float(b.high),
            low=float(b.low),
            close=float(b.close),
            volume=float(b.volume),
        )
        for b in bars
    ]


@router.post("/signals/rescore", response_model=list[SignalOut])
def rescore(session: SessionDep) -> list[SignalOut]:
    """Recompute signals for every instrument from stored data.

    Safe to call repeatedly: it writes new signal rows rather than mutating
    old ones, so the history of what was judged when stays intact.
    """
    scoring_service.score_all(session)
    return list_signals(session)
