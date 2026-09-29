"""목록 날 기준 전 거래일 상한가 여부(표시·사후 기록 전용). 읽기만 한다.

목록 날 D의 직전 한국 세션 P의 일봉과, 그보다 앞선 가장 최근 일봉(기준가격 = 그 종가)으로 판정한다. P의 봉이 없으면
(사전 수집 상한·실패, 거래정지) 판정하지 않는다. 봉은 P 마감 뒤 값이라 D 개장 전에 알 수 있다. 화면은 목록을 얼린
시각까지 들어온 수정본만 읽는다(나중에 들어온 값으로 지난 화면을 바꾸지 않게).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.calendar import Market, MarketCalendar
from app.core.types import Interval
from app.repositories import candle_repo
from app.scoring.price_limit import state

KR = MarketCalendar(Market.KR)
SEOUL = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class PrevLimit:
    state: str | None
    """LOCKED / CLOSED / TOUCHED, 아니면 None."""
    change_pct: float
    """전 거래일 종가 등락(%)."""
    day: date
    close: float


def previous_session(day: date) -> date | None:
    before = KR.sessions_between(day - timedelta(days=14), day - timedelta(days=1))
    return before[-1] if before else None


def prev_limits(
    session: Session,
    day: date,
    instrument_ids: Collection[int],
    *,
    ingested_before: datetime | None = None,
) -> dict[int, PrevLimit]:
    p = previous_session(day)
    if p is None:
        return {}
    closed = KR.session_close(p)
    out: dict[int, PrevLimit] = {}
    for i in instrument_ids:
        bars = candle_repo.history(
            session,
            i,
            Interval.DAY_1,
            limit=2,
            available_before=closed,
            ingested_before=ingested_before,
        )
        if len(bars) < 2 or bars[-1].ts.astimezone(SEOUL).date() != p:
            continue
        base, bar = bars[-2], bars[-1]
        if base.close <= 0:
            continue
        out[i] = PrevLimit(
            state=state(bar.open, bar.high, bar.low, bar.close, base.close),
            change_pct=round((float(bar.close) / float(base.close) - 1) * 100, 2),
            day=p,
            close=float(bar.close),
        )
    return out
