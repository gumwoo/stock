"""아침 목록 브리핑(카카오톡 나에게 보내기용 문장). 읽기만 한다.

데스크톱 앱의 예약 작업이 평일 08:44(cron 08:40 + 앱 지연 4분)에 이 주소를 받아 `messages`를 순서대로 그대로 보낸다. 날짜는 서버가 정한다(한국 날짜).
보내는 창은 08:40~09:30이다(08:38 목록이 끝나기 전에 불러 거짓 "목록 없음"이 가지 않게 08:40부터): 앱이 늦게 켜져 예약이 늦게 돌면 아침 브리핑이 엉뚱한 시각에 가거나, 목록 전이라 목록이
아직 없는데 "목록 없음"이 가지 않게 창 밖에서는 빈 목록을 준다. `force=true`는 시험 발송용(창과 날짜를 무시).
"""

from __future__ import annotations

import logging
import time as time_module
from datetime import date, datetime, time
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.lists import signal_rows
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.db import get_db
from app.models import ExpectedOpen, WatchlistMember, WatchlistSnapshot
from app.realtime.gateway import load_members, member_dict
from app.scoring.gap import DECIDE_BY, GAP_UP
from app.scoring.watchlist import LOW_SCORE, PREV_SURGE, STRATEGY_VERSION_V2
from app.services import briefing_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/briefing", tags=["briefing"])
SessionDep = Annotated[Session, Depends(get_db)]
SEOUL = ZoneInfo("Asia/Seoul")
KR = MarketCalendar(Market.KR)
WINDOW = (time(8, 40), time(9, 30))
GAP_WINDOW = (time(8, 50), time(9, 10))
GAP_WAIT_SECONDS = 50


@router.get("")
def briefing(
    session: SessionDep,
    day: date | None = None,
    force: bool = False,
    x_briefing_part: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """`X-Briefing-Part: gap`이면 9시 전 예상 갭 판정 메시지(08:54쯤 두 번째 예약 작업). 응답의 `part`로 어느 쪽인지 알린다 —
    작업은 `part`가 gap이 아니면 보내지 않는다(헤더가 빠져 전체 브리핑이 두 번 가지 않게)."""
    now = utc_now()
    local = now.astimezone(SEOUL)
    target = day if (force and day is not None) else KR.local_today(now)
    part = "gap" if (x_briefing_part or "").strip().lower() == "gap" else "morning"
    if not KR.is_session(target):
        return {"day": target.isoformat(), "part": part, "status": "HOLIDAY", "messages": []}
    if part == "gap":
        return {"day": target.isoformat(), "part": part, **_gap(session, target, force=force)}
    if not force and not (WINDOW[0] <= local.time() <= WINDOW[1] and local.date() == target):
        status = "NOT_YET" if local.time() < WINDOW[0] else "LATE"
        return {"day": target.isoformat(), "part": part, "status": status, "messages": []}
    snap = session.execute(
        select(WatchlistSnapshot).where(
            WatchlistSnapshot.session_date == target,
            WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
        )
    ).scalar_one_or_none()
    if snap is None:
        return {
            "day": target.isoformat(),
            "part": part,
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
        "part": part,
        "status": "OK",
        "messages": briefing_service.build(
            target,
            rows,
            events,
            _count(session, snap.id, (LOW_SCORE, PREV_SURGE)),
            _count(session, snap.id, (GAP_UP,)),
        ),
    }


def _count(session: Session, snapshot_id: int, reasons: tuple[str, ...]) -> int:
    """그날 목록에서 이 이유들로 뺀 종목 수."""
    return int(
        session.execute(
            select(func.count()).where(
                WatchlistMember.snapshot_id == snapshot_id,
                or_(*(WatchlistMember.excluded_reason.contains(r) for r in reasons)),
            )
        ).scalar_one()
    )


def _snapshot(session: Session, day: date) -> WatchlistSnapshot | None:
    return session.execute(
        select(WatchlistSnapshot).where(
            WatchlistSnapshot.session_date == day,
            WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
        )
    ).scalar_one_or_none()


def _gap(session: Session, day: date, *, force: bool) -> dict[str, Any]:
    """08:50 예상 갭 판정 결과. 판정이 아직 진행 중이면(08:54 전) 최대 50초 기다린다 — 거짓 "판정 못 함"이 가지 않게."""
    local = utc_now().astimezone(SEOUL)
    if not force and not (GAP_WINDOW[0] <= local.time() <= GAP_WINDOW[1] and local.date() == day):
        return {"status": "NOT_YET" if local.time() < GAP_WINDOW[0] else "LATE", "messages": []}
    snap = _snapshot(session, day)
    waited = 0
    while not force and (snap is None or "gap_check" not in (snap.inputs or {})):
        now_local = utc_now().astimezone(SEOUL)
        if waited >= GAP_WAIT_SECONDS or now_local.time() >= DECIDE_BY.replace(second=50):
            break
        time_module.sleep(5)
        waited += 5
        session.expire_all()
        snap = _snapshot(session, day)
    if snap is None:
        return {"status": "NO_LIST", "messages": []}
    found = (snap.inputs or {}).get("gap_check")
    check: dict[str, Any] = found if isinstance(found, dict) else {}
    if not check:
        return {
            "status": "NO_CHECK",
            "messages": briefing_service.gap_warning(day, "08:50 판정 기록 없음"),
        }
    if not check.get("applied"):
        return {
            "status": "NO_CHECK",
            "messages": briefing_service.gap_warning(day, "08:54 전에 끝나지 않음"),
        }
    if not int(check.get("judgeable") or 0):
        # 조회는 됐지만 판정할 수 있는 값이 하나도 없다(장전 응답 모양이 예상과 다름 등) — "갭업 없음"으로 읽히지 않게.
        why = f"조회 {check.get('checked', 0)}·실패 {check.get('failed', 0)}·불일치 {check.get('mismatched', 0)}, 판정할 값 없음"
        return {"status": "NO_CHECK", "messages": briefing_service.gap_warning(day, why)}
    before = signal_rows(session, day, snap, with_gap_up=True)
    reasons = dict(
        session.execute(
            select(WatchlistMember.instrument_id, WatchlistMember.excluded_reason).where(
                WatchlistMember.snapshot_id == snap.id
            )
        )
        .tuples()
        .all()
    )
    after = [r for r in before if reasons.get(int(r["instrument_id"])) != GAP_UP]
    gapped_ids = [
        int(r["instrument_id"]) for r in before if reasons.get(int(r["instrument_id"])) == GAP_UP
    ]
    pct = _latest_pct(session, day, gapped_ids, str(check["at"]))
    names = {int(r["instrument_id"]): str(r["name"]) for r in before}
    gapped = [(names[i], pct.get(i)) for i in gapped_ids]
    return {
        "status": "OK" if gapped else "NONE",
        "messages": briefing_service.gap_messages(
            day,
            check,
            gapped,
            briefing_service.rankings(before),
            briefing_service.rankings(after),
            len(after),
        ),
    }


def _latest_pct(
    session: Session, day: date, ids: list[int], check_at: str
) -> dict[int, float | None]:
    if not ids:
        return {}
    rows = session.execute(
        select(ExpectedOpen.instrument_id, ExpectedOpen.change_pct).where(
            ExpectedOpen.session_date == day,
            ExpectedOpen.instrument_id.in_(ids),
            ExpectedOpen.check_at == datetime.fromisoformat(check_at),
        )
    ).all()
    return {int(i): (float(p) if p is not None else None) for i, p in rows}
