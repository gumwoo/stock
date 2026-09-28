"""밤사이 미국 반도체: 한국 거래일 D 개장 전에 끝난 미국 세션들의 등락(화면 참고용). 읽기만 한다.

정렬은 밤사이 연구(`app/scoring/overnight_study.align`)와 같다 — (D 개장 전 마지막 미국 종가) / (D 전 한국 세션 마감 전
마지막 미국 종가) - 1. 그 사이 새 미국 세션이 없으면 없음, 여럿이면(한국 연휴 뒤) 누적. 미국 종가 시각은 일봉의
`available_at`(그 세션 마감). 주식(NVDA·MU)은 분할이 섞이면 값이 튀므로 |등락| > 30%면 숨기고 "분할 의심"으로 둔다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.market_index import OVERNIGHT_REFERENCES
from app.core.calendar import Market, MarketCalendar
from app.models.market import MarketIndexBar
from app.scoring.overnight_study import align

KR = MarketCalendar(Market.KR)
NEW_YORK = ZoneInfo("America/New_York")
LABELS = {"^SOX": "필라델피아 반도체", "NVDA": "엔비디아", "MU": "마이크론"}
SPLIT_SUSPECT = 0.30
INDEX_CODES = frozenset({"^SOX"})


def kr_day(day: date) -> date:
    """고른 날이 휴장일이면 그다음 한국 세션."""
    return KR.session_on_or_after(day)


def us_semis(session: Session, day: date) -> dict[str, Any]:
    d = kr_day(day)
    prev = KR.sessions_between(d - timedelta(days=14), d - timedelta(days=1))
    if not prev:
        return {"day": d.isoformat(), "refs": []}
    open_at = KR.session_open(d)
    prev_close = KR.session_close(prev[-1])
    refs: list[dict[str, Any]] = []
    for code in OVERNIGHT_REFERENCES:
        bars = session.execute(
            select(MarketIndexBar.available_at, MarketIndexBar.close)
            .where(
                MarketIndexBar.index_code == code,
                MarketIndexBar.available_at < open_at,
                MarketIndexBar.available_at >= prev_close - timedelta(days=21),
            )
            .order_by(MarketIndexBar.available_at)
        ).all()
        refs.append(reference(code, [(t, float(c)) for t, c in bars], open_at, prev_close))
    return {"day": d.isoformat(), "refs": refs}


def reference(
    code: str, series: list[tuple[datetime, float]], open_at: datetime, prev_close: datetime
) -> dict[str, Any]:
    """한 지표의 밤사이 등락. 순수. series는 (미국 종가 시각, 종가), 시각 순."""
    d = open_at.date()
    r = align([d], {d: open_at}, {d: prev_close}, series)[d]
    sessions = sorted(
        {t.astimezone(NEW_YORK).date() for t, _ in series if prev_close <= t < open_at}
    )
    suspect = r is not None and code not in INDEX_CODES and abs(r) > SPLIT_SUSPECT
    return {
        "code": code,
        "label": LABELS.get(code, code),
        "change_pct": None if r is None or suspect else round(r * 100, 2),
        "split_suspect": suspect,
        "us_sessions": [s.isoformat() for s in sessions],
    }
