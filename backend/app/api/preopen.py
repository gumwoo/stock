"""아침 흐름 상태 API: 다음(또는 오늘) 아침의 후보 풀과 단계별 상태. 읽기만 한다.

"오늘의 관찰" 화면이 목록이 없거나 전날 목록이 남아 있을 때 "다음 목록은 언제이고, 아침 단계가 어디까지
왔나"를 보여 주려고 쓴다. 게이트웨이(실시간 목록)와 따로 DB에서 읽으므로 실시간 연결이 꺼져 있어도 보인다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.db import get_db
from app.services import preopen_service

router = APIRouter(prefix="/api", tags=["preopen"])
SessionDep = Annotated[Session, Depends(get_db)]
SEOUL = ZoneInfo("Asia/Seoul")

# (단계 이름, 화면 이름, 예정 시각). 순서는 실제 체인 순서(preopen_service.run_morning → run_supplement →
# run_scores → take_snapshot).
STAGES: tuple[tuple[str, str, str], ...] = (
    (preopen_service.SWEEP, "뉴스·공시 수집", "07:00"),
    (preopen_service.POOL, "후보 풀 확정", "07:00"),
    (preopen_service.SEARCH_TRENDS, "검색 추세", "07:00"),
    (preopen_service.PREFETCH, "가격·재무 사전 수집", "07:00"),
    (preopen_service.LLM, "기사 판정·해석(LLM)", "07:00"),
    (preopen_service.THEME_NEWS, "테마 뉴스", "07:00"),
    (preopen_service.SUPPLEMENT, "아침 기사 보충", "08:30"),
    (preopen_service.SUPPLEMENT_LLM, "보충 기사 해석(LLM)", "08:30"),
    (preopen_service.THEME_REFRESH, "테마 뉴스 갱신", "08:30"),
    (preopen_service.SCORE, "관찰용 점수", "08:40"),
    (preopen_service.SNAPSHOT, "관찰 목록", "08:50"),
)
SLOW_AFTER = timedelta(minutes=60)
NOTE_CHARS = 120
NOTED = frozenset({preopen_service.FAILED, preopen_service.SKIPPED, preopen_service.PARTIAL})


def stage_rows(
    stages: Mapping[str, Any] | None, *, now: datetime, opened: bool
) -> list[dict[str, Any]]:
    """풀의 `stages` JSON을 화면용 행으로. 없는 단계는 status None.

    RUNNING인데 시작한 지 60분이 지났으면 "SLOW"(오래 걸림). 워커가 도중에 죽으면 RUNNING이 영원히 남지만,
    07:00 LLM처럼 정상적으로 오래 걸리는 단계도 있어 "멈춤"이라고 단정하지 않는다.
    장이 열렸는데 목록 단계 기록이 없으면 "MISSING". 목록은 다 만든 뒤에만 기록되고 개장 뒤에는 만들지 않으므로,
    08:50이 아니라 개장 시각부터 실패가 확정된다(그 전에는 만드는 중일 수 있다).
    note는 실패·건너뜀·일부일 때만 앞 120자(예외 문자열을 화면에 그대로 늘어놓지 않는다).
    """
    rows = []
    for name, label, at in STAGES:
        entry = (stages or {}).get(name)
        status = entry.get("status") if isinstance(entry, dict) else None
        note = None
        if isinstance(entry, dict) and status in NOTED and entry.get("detail"):
            note = str(entry["detail"])[:NOTE_CHARS]
        if status == preopen_service.RUNNING and isinstance(entry, dict):
            started = entry.get("started_at")
            if started and now - datetime.fromisoformat(str(started)) > SLOW_AFTER:
                status = "SLOW"
        if status is None and name == preopen_service.SNAPSHOT and opened:
            status = "MISSING"
        rows.append({"name": name, "label": label, "at": at, "status": status, "note": note})
    return rows


@router.get("/preopen/today")
def preopen_today(session: SessionDep) -> dict[str, Any]:
    now = utc_now()
    day = preopen_service.morning_day(now)
    list_at = datetime.combine(day, preopen_service.LIST_AT, tzinfo=SEOUL)
    list_passed = now >= list_at
    opened = now >= preopen_service.KR.session_open(day)
    pool = preopen_service.pool_for(session, day)
    return {
        "now": now.isoformat(),
        "day": day.isoformat(),
        "list_at": list_at.isoformat(),
        "list_passed": list_passed,
        "opened": opened,
        "pool": None
        if pool is None
        else {
            "status": pool.status,
            "pool_count": pool.pool_count,
            "asof": pool.asof.isoformat() if pool.asof else None,
        },
        "stages": stage_rows(pool.stages if pool is not None else None, now=now, opened=opened),
    }
