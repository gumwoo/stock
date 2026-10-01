"""아침 목록 브리핑(카카오톡 나에게 보내기용 문장). 읽기만 한다.

데스크톱 앱의 예약 작업이 평일 08:44(cron 08:40 + 앱 지연 4분)에 이 주소를 받아 `messages`를 순서대로 그대로 보낸다. 날짜는 서버가 정한다(한국 날짜).
보내는 창은 08:40~09:30이다(08:38 목록이 끝나기 전에 불러 거짓 "목록 없음"이 가지 않게 08:40부터): 앱이 늦게 켜져 예약이 늦게 돌면 아침 브리핑이 엉뚱한 시각에 가거나, 목록 전이라 목록이
아직 없는데 "목록 없음"이 가지 않게 창 밖에서는 빈 목록을 준다. `force=true`는 시험 발송용(창과 날짜를 무시).
"""

from __future__ import annotations

import logging
from datetime import date, time
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.lists import signal_rows
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.db import get_db
from app.models import WatchlistSnapshot
from app.realtime.gateway import load_members, member_dict
from app.scoring.watchlist import STRATEGY_VERSION_V2
from app.services import briefing_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/briefing", tags=["briefing"])
SessionDep = Annotated[Session, Depends(get_db)]
SEOUL = ZoneInfo("Asia/Seoul")
KR = MarketCalendar(Market.KR)
WINDOW = (time(8, 40), time(9, 30))


@router.get("")
def briefing(session: SessionDep, day: date | None = None, force: bool = False) -> dict[str, Any]:
    now = utc_now()
    local = now.astimezone(SEOUL)
    target = day if (force and day is not None) else KR.local_today(now)
    if not KR.is_session(target):
        return {"day": target.isoformat(), "status": "HOLIDAY", "messages": []}
    if not force and not (WINDOW[0] <= local.time() <= WINDOW[1] and local.date() == target):
        status = "NOT_YET" if local.time() < WINDOW[0] else "LATE"
        return {"day": target.isoformat(), "status": status, "messages": []}
    snap = session.execute(
        select(WatchlistSnapshot).where(
            WatchlistSnapshot.session_date == target,
            WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
        )
    ).scalar_one_or_none()
    if snap is None:
        return {
            "day": target.isoformat(),
            "status": "NO_LIST",
            "messages": [
                f"[{target.month}/{target.day}] 아침 목록이 없습니다(서버·워커 확인 필요)"
            ],
        }
    rows = signal_rows(session, target, snap)
    events: dict[int, list[dict[str, Any]]] = {}
    try:  # 뉴스·공시는 보조다: 실패해도 점수 순위는 보낸다
        _, members = load_members(target)
        events = {m.instrument_id: member_dict(m)["events"] for m in members}
        # 공시 사건과 쉬운 설명은 load_members(event_brief_service)가 붙인다.
    except Exception:
        logger.exception("briefing: news and disclosures failed; scores only")
    return {
        "day": target.isoformat(),
        "status": "OK",
        "messages": briefing_service.build(target, rows, events),
    }
