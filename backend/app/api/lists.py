"""날짜별 아침 목록: 그날 목록 종목의 신호(개장 전 채점), 관찰 목록(뉴스 근거·링크), 저장된 1분봉·1초봉. 읽기만 한다.

신호 탭과 오늘의 관찰이 날짜를 골라 지난날도 보게 하려는 것이다. 신호는 그날 V2 목록(개장 전에 얼린 행)의 점수와
점수 상세(`score_detail`)를 그대로 보여 준다. 새로 계산하지 않는다.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Instrument, WatchlistMember, WatchlistSnapshot
from app.realtime.gateway import load_members, member_dict
from app.repositories import instrument_repo, minute_repo
from app.scoring.policy import THRESHOLDS
from app.scoring.watchlist import STRATEGY_VERSION_V2
from app.services import (
    analyst_service,
    briefing_service,
    heavyweight_service,
    price_limit_service,
)
from app.services.heavyweight_service import Weight
from app.services.price_limit_service import PrevLimit

router = APIRouter(prefix="/api/lists", tags=["lists"])
SessionDep = Annotated[Session, Depends(get_db)]
DAYS_SHOWN = 60
logger = logging.getLogger(__name__)


def _snapshot(session: Session, day: date) -> WatchlistSnapshot:
    snap = session.execute(
        select(WatchlistSnapshot).where(
            WatchlistSnapshot.session_date == day,
            WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
        )
    ).scalar_one_or_none()
    if snap is None:
        raise HTTPException(status_code=404, detail=f"no morning list for {day}")
    return snap


@router.get("/days")
def list_days(session: SessionDep) -> list[str]:
    """아침 목록(V2)이 있는 날짜, 최신 먼저."""
    rows = session.execute(
        select(WatchlistSnapshot.session_date)
        .where(WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2)
        .order_by(WatchlistSnapshot.session_date.desc())
        .limit(DAYS_SHOWN)
    ).scalars()
    return [d.isoformat() for d in rows]


def signal_row(
    m: WatchlistMember,
    name: str,
    code: str | None,
    weight: Weight | None = None,
    limit: PrevLimit | None = None,
    analyst: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """목록 행 하나를 신호 탭의 행으로. 순수하다.

    `weight_total`(참여한 요인 가중치 합)과 `thresholds`(판단 기준)를 함께 준다. 판단은 합계를 가중치 합으로 나눈 값을
    기준에 대는 것과 같다(`Thresholds.action_for`) — 재무가 없으면 기술 점수 하나로 판단한다. 화면이 기준값을 상수로
    들고 있지 않게 여기서 준다. 기준값은 지금 규칙의 것이다(전략 버전이 하나뿐인 동안은 행의 채점 당시와 같다).
    """
    weight_total = briefing_service.weight_total(m.score_detail)
    return {
        "member_id": m.id,
        "instrument_id": m.instrument_id,
        "code": code,
        "name": name,
        "rank": m.rank,
        "list_reasons": list(m.reasons),
        "total_score": m.total_score,
        "action": m.last_action,
        "technical_score": m.technical_score,
        "fundamental_score": m.fundamental_score,
        "prefetch_status": m.prefetch_status,
        "abstained_reason": m.abstained_reason,
        "regime": m.regime,
        "overlay_points": m.overlay_points,
        "attention_surge": m.attention_surge,
        "evaluated_at": m.evaluated_at.isoformat() if m.evaluated_at else None,
        "detail": m.score_detail,
        "weight_total": weight_total,
        "thresholds": {"buy_interest": THRESHOLDS.buy_interest, "caution": THRESHOLDS.caution},
        # 지수 대형주(표시 전용): 목록 날 이전 순위표 기준. 목록 선정·채점에는 쓰지 않는다.
        **weight_fields(weight),
        # 전일 상한가(표시 전용): 전 거래일 일봉 기준. 목록 선정·채점에는 쓰지 않는다.
        **limit_fields(limit),
        # 증권사 투자의견(참고, KIS). 조회하지 못한 종목은 null, 조회했는데 리포트가 없으면 count 0.
        "analyst": analyst,
    }


def _weights(session: Session, day: date, ids: list[int]) -> dict[int, Weight]:
    """대형주 표시는 보조다. 읽다 실패하면 표시 없이 신호를 그대로 보인다(세이브포인트로 트랜잭션을 지킨다)."""
    try:
        with session.begin_nested():
            return heavyweight_service.weights_for(session, day, ids)
    except Exception:
        logger.exception("list signals: market weights failed; no heavyweight labels")
        return {}


def limit_fields(limit: PrevLimit | None) -> dict[str, Any]:
    return {
        "prev_limit": limit.state if limit else None,
        "prev_change_pct": limit.change_pct if limit else None,
    }


def _limits(
    session: Session, day: date, ids: list[int], asof: datetime | None
) -> dict[int, PrevLimit]:
    """전일 상한가 표시는 보조다. 읽다 실패하면 표시 없이 신호를 그대로 보인다."""
    try:
        with session.begin_nested():
            return price_limit_service.prev_limits(session, day, ids, ingested_before=asof)
    except Exception:
        logger.exception("list signals: prior-day limits failed; no limit labels")
        return {}


def _analysts(
    session: Session, day: date, ids: list[int], limits: dict[int, PrevLimit]
) -> dict[int, dict[str, Any]]:
    """증권사 의견 표시는 보조다. 읽다 실패하면 표시 없이 신호를 그대로 보인다."""
    try:
        with session.begin_nested():
            return analyst_service.summaries(
                session, day, ids, closes={i: x.close for i, x in limits.items()}
            )
    except Exception:
        logger.exception("list signals: analyst opinions failed; no opinion lines")
        return {}


def weight_fields(weight: Weight | None) -> dict[str, Any]:
    return {
        "market_weight_pct": weight.weight_pct if weight else None,
        "market_listing": weight.listing if weight else None,
        "heavyweight": bool(weight and weight.heavyweight),
        "sector": weight.sector if weight else None,
    }


@router.get("/{day}/signals")
def list_signals(day: date, session: SessionDep) -> list[dict[str, Any]]:
    """그날 목록 종목의 아침 점수와 상세. 목록 순위 순(정렬은 화면이 한다)."""
    return signal_rows(session, day, _snapshot(session, day))


def signal_rows(session: Session, day: date, snap: WatchlistSnapshot) -> list[dict[str, Any]]:
    """신호 탭의 행들. 아침 브리핑(`app/api/briefing.py`)도 같은 행을 쓴다."""
    rows = session.execute(
        select(WatchlistMember, Instrument.name)
        .join(Instrument, Instrument.instrument_id == WatchlistMember.instrument_id)
        .where(WatchlistMember.snapshot_id == snap.id)
        .order_by(WatchlistMember.rank)
    ).all()
    ids = [m.instrument_id for m, _ in rows]
    weights = _weights(session, day, ids)
    # 목록을 얼린 시각까지 들어온 봉만(나중에 받은 봉으로 지난 화면을 바꾸지 않게).
    limits = _limits(session, day, ids, snap.created_at)
    analysts = _analysts(session, day, ids, limits)
    return [
        signal_row(
            m,
            name,
            instrument_repo.current_symbol(session, m.instrument_id),
            weights.get(m.instrument_id),
            limits.get(m.instrument_id),
            analysts.get(m.instrument_id),
        )
        for m, name in rows
    ]


@router.get("/{day}/members")
def list_members(day: date, session: SessionDep) -> dict[str, Any]:
    """그날 관찰 목록(근거 뉴스·공시와 링크 포함). 실시간 화면의 목록과 같은 모양이고 시세(`last`)는 없다."""
    _snapshot(session, day)  # 그날 V2 목록이 없으면 404(추적 종목 폴백·V1로 새지 않게)
    source, members = load_members(day)
    return {"day": day.isoformat(), "source": source, "members": [member_dict(m) for m in members]}


@router.get("/{day}/bars/{instrument_id}")
def list_bars(
    day: date, instrument_id: int, session: SessionDep, interval: str = "1m"
) -> list[dict[str, Any]]:
    """그날 저장된 봉. 1m은 장 마감 뒤 REST로 받은 1분봉(16:20 전에는 비어 있다), 1s는 실시간 연결로 저장한 1초봉."""
    if interval not in ("1m", "1s"):
        raise HTTPException(status_code=400, detail="interval is 1m or 1s")
    bars: list[Any] = (
        minute_repo.bars_for(session, instrument_id, day)
        if interval == "1m"
        else minute_repo.second_bars_for(session, instrument_id, day)
    )
    return [
        {
            "time": int(b.ts.timestamp()),
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": float(b.volume),
        }
        for b in bars
    ]
