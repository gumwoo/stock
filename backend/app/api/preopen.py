"""아침 흐름 상태 API: 다음(또는 오늘) 아침의 후보 풀과 단계별 상태. 읽기만 한다.

"오늘의 관찰" 화면이 목록이 없거나 전날 목록이 남아 있을 때 "다음 목록은 언제이고, 아침 단계가 어디까지
왔나"를 보여 주려고 쓴다. 게이트웨이(실시간 목록)와 따로 DB에서 읽으므로 실시간 연결이 꺼져 있어도 보인다.
"""

from __future__ import annotations

import re
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

# --- 단계 메모를 화면용 한국어로 -------------------------------------------------------------------
# 풀의 `stages[..].detail`은 로그·진단용 영어 원문이고 DB에 그대로 남는다. 화면에는 알려진 형식만 한국어로 풀고,
# 모르는 형식(수집기 자유 문구, 예외 메시지)은 원문을 늘어놓지 않고 어디에 남아 있는지만 말한다.
# 형식은 preopen_service.py·llm_service.py·collectors/theme_news.py의 f-string을 그대로 옮긴 것이다.

UNKNOWN_NOTE = "원문은 아침 단계 기록(DB)에 남아 있습니다"
ERROR_NOTE = "예상하지 못한 오류로 멈춤 · " + UNKNOWN_NOTE
RUN_STATUS = {"SUCCESS": "완료", "PARTIAL": "일부만", "SKIPPED": "건너뜀", "FAILED": "실패"}
PREFETCH_KEY = {
    preopen_service.FETCHED: "새로 받음",
    preopen_service.FRESH: "이미 최신",
    preopen_service.NO_DATA: "데이터 없음",
    preopen_service.SKIPPED_CAP: "하루 상한으로 못 받음",
    preopen_service.FAILED: "실패",
}
SKIP_REASON = {
    "pool was not frozen": "후보 풀이 확정되지 않아 건너뜀",
    "the list is already frozen": "목록이 이미 확정되어 건너뜀",
    "past the open": "장이 이미 열려 건너뜀",
}


def _kst(hour: str, minute: str) -> str:
    """원문의 "HH:MMZ"(UTC)를 서울 시각으로. 자정을 넘으면 24로 나눈 나머지."""
    return f"{(int(hour) + 9) % 24:02d}:{minute}"


def _stopped(reason: str) -> str:
    """LLM 배치가 멈춘 이유(llm_service.py의 report.stopped). 공급자 문구 같은 뒷부분은 버린다."""
    if reason.startswith("another LLM run is in progress"):
        return "다른 해석 작업이 돌고 있어 멈춤"
    usage = re.match(r"(five-hour|seven-day) usage at (\d+)%", reason)
    if usage:
        window = "5시간" if usage[1] == "five-hour" else "7일"
        if "not starting" in reason:
            return f"{window} 사용량 {usage[2]}%라 시작하지 않음"
        return f"{window} 사용량 {usage[2]}%에서 멈춤"
    if reason.startswith("call quota:"):
        return "호출 한도로 멈춤"
    if reason.startswith("subscription refused:"):
        return "구독에서 거절되어 멈춤"
    if reason.startswith("unavailable:"):
        return "LLM을 쓸 수 없어 멈춤"
    return "도중에 멈춤"


def _llm(detail: str) -> str | None:
    if detail == "LLM_SCHEDULE_ENABLED is off":
        return "예약 해석이 꺼져 있음"
    m = re.fullmatch(r"(\d+) of (\d+) items(?:; stopped: (.*))?", detail, re.DOTALL)
    if not m:
        return None
    # items는 판정·해석을 합쳐 모델에 보낸 기사 수다(형식이 틀린 답이 온 묶음 포함). "해석한 건수"가 아니다.
    text = f"모델에 보낸 기사 {m[1]}/{m[2]}건"
    return f"{text} · {_stopped(m[3])}" if m[3] is not None else text


def _prefetch(detail: str) -> str | None:
    parts = []
    for item in detail.split(", "):
        m = re.fullmatch(r"(\w+) (\d+)", item)
        if not m or m[1] not in PREFETCH_KEY:
            return None  # 모르는 키가 하나라도 있으면 원문 대신 폴백
        parts.append(f"{PREFETCH_KEY[m[1]]} {m[2]}")
    return " · ".join(parts)


def _theme(detail: str) -> str | None:
    m = re.fullmatch(
        r"(\d+)/(\d+) themes since \d{4}-\d{2}-\d{2} \d{2}:\d{2}Z for \d{4}-\d{2}-\d{2}"
        r"(; capped at 1,000: .*?)?(; stopped: .*)?",
        detail,
        re.DOTALL,
    )
    if not m:
        return None
    text = f"테마 {m[2]}개 중 {m[1]}개 수집"
    if m[3]:
        text += " · 일부 테마는 1,000건 상한에 걸림"
    if m[4]:
        text += " · 도중에 멈춤"
    return text


def _for_stage(name: str, detail: str) -> str | None:
    s = preopen_service
    if name == s.SWEEP:
        m = re.fullmatch(r"news (\w+), disclosures (\w+)", detail)
        if m and m[1] in RUN_STATUS and m[2] in RUN_STATUS:
            return f"뉴스 {RUN_STATUS[m[1]]}, 공시 {RUN_STATUS[m[2]]}"
    elif name == s.POOL:
        m = re.fullmatch(r"already frozen at (\d{2}):(\d{2})Z", detail)
        if m:
            return f"이미 {_kst(m[1], m[2])}에 확정됨"
    elif name == s.SEARCH_TRENDS:
        m = re.fullmatch(r"(\d+) names, (\w+)", detail)
        if m and m[2] in RUN_STATUS:
            return f"{m[1]}종목, {RUN_STATUS[m[2]]}"
    elif name == s.PREFETCH:
        return _prefetch(detail)
    elif name in (s.LLM, s.SUPPLEMENT_LLM):
        return _llm(detail)
    elif name in (s.THEME_NEWS, s.THEME_REFRESH):
        return RUN_STATUS.get(detail) or _theme(detail)
    elif name == s.SUPPLEMENT:
        m = re.fullmatch(r"(\d+) names since (\d{2}):(\d{2})Z, (\w+)", detail)
        if m and m[4] in RUN_STATUS:
            return f"{m[1]}종목, {_kst(m[2], m[3])} 이후 기사, {RUN_STATUS[m[4]]}"
    elif name == s.SCORE:
        m = re.fullmatch(r"(\d+) scored, (\d+) without bars, (\d+) failed; (\d+) peers", detail)
        if m:
            return f"점수 {m[1]} · 일봉 없음 {m[2]} · 실패 {m[3]} · 비교군 {m[4]}"
    return None


def korean_note(name: str, detail: str) -> str:
    """단계 `name`의 원문 메모를 화면용 한국어로. 알려진 형식이 아니면 폴백 문구."""
    known = _for_stage(name, detail)
    if known is not None:
        return known
    if detail in SKIP_REASON:
        return SKIP_REASON[detail]
    m = re.fullmatch(r"prerequisite not finished by (\d{2}:\d{2}): (.+)", detail)
    if m:
        labels = {n: label for n, label, _ in STAGES}
        names = m[2].split(", ")
        if all(n in labels for n in names):
            return f"{m[1]}까지 앞 단계가 끝나지 않아 건너뜀: {', '.join(labels[n] for n in names)}"
        return UNKNOWN_NOTE
    # _step이 남기는 예외 "{Type}: {msg}". 예외 문구는 화면에 늘어놓지 않는다(원문은 DB에 있다).
    if re.match(r"[A-Z][A-Za-z0-9_.]*: ", detail):
        return ERROR_NOTE
    return UNKNOWN_NOTE


def _parse(value: object) -> datetime | None:
    """단계 기록의 시각. 읽을 수 없거나 시간대가 없으면 None(화면 하나 때문에 API 전체가 실패하지 않게)."""
    try:
        at = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return at if at is not None and at.tzinfo is not None else None


def stage_rows(
    stages: Mapping[str, Any] | None, *, now: datetime, opened: bool
) -> list[dict[str, Any]]:
    """풀의 `stages` JSON을 화면용 행으로. 없는 단계는 status None.

    RUNNING인데 시작한 지 60분이 지났으면 "SLOW"(오래 걸림). 워커가 도중에 죽으면 RUNNING이 영원히 남지만,
    07:00 LLM처럼 정상적으로 오래 걸리는 단계도 있어 "멈춤"이라고 단정하지 않는다.
    장이 열렸는데 목록 단계 기록이 없으면 "MISSING". 목록은 다 만든 뒤에만 기록되고 개장 뒤에는 만들지 않으므로,
    08:50이 아니라 개장 시각부터 실패가 확정된다(그 전에는 만드는 중일 수 있다).
    note는 실패·건너뜀·일부일 때만, `korean_note`로 한국어로 바꾼 앞 120자(예외 문자열을 화면에 그대로
    늘어놓지 않는다).
    """
    rows = []
    for name, label, at in STAGES:
        entry = (stages or {}).get(name)
        status = entry.get("status") if isinstance(entry, dict) else None
        note = None
        if isinstance(entry, dict) and status in NOTED and entry.get("detail"):
            note = korean_note(name, str(entry["detail"]))[:NOTE_CHARS]
        if status == preopen_service.RUNNING and isinstance(entry, dict):
            started = _parse(entry.get("started_at"))
            if started is not None and now - started > SLOW_AFTER:
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
