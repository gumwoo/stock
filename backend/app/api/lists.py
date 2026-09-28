"""날짜별 아침 목록: 그날 목록 종목의 신호(08:40 채점), 관찰 목록(뉴스 근거·링크), 저장된 1분봉·1초봉. 읽기만 한다.

신호 탭과 오늘의 관찰이 날짜를 골라 지난날도 보게 하려는 것이다. 신호는 그날 V2 목록(08:50에 얼린 행)의 점수와
점수 상세(`score_detail`)를 그대로 보여 준다. 새로 계산하지 않는다.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Instrument, WatchlistMember, WatchlistSnapshot
from app.realtime.gateway import load_members, member_dict
from app.repositories import instrument_repo, minute_repo
from app.scoring.watchlist import STRATEGY_VERSION_V2

router = APIRouter(prefix="/api/lists", tags=["lists"])
SessionDep = Annotated[Session, Depends(get_db)]
DAYS_SHOWN = 60


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


def signal_row(m: WatchlistMember, name: str, code: str | None) -> dict[str, Any]:
    """목록 행 하나를 신호 탭의 행으로. 순수하다."""
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
    }


@router.get("/{day}/signals")
def list_signals(day: date, session: SessionDep) -> list[dict[str, Any]]:
    """그날 목록 종목의 08:40 점수와 상세. 목록 순위 순(정렬은 화면이 한다)."""
    snap = _snapshot(session, day)
    rows = session.execute(
        select(WatchlistMember, Instrument.name)
        .join(Instrument, Instrument.instrument_id == WatchlistMember.instrument_id)
        .where(WatchlistMember.snapshot_id == snap.id)
        .order_by(WatchlistMember.rank)
    ).all()
    return [
        signal_row(m, name, instrument_repo.current_symbol(session, m.instrument_id))
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
