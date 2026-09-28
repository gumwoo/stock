"""목록 날 기준 지수 대형주 판정(표시·사후 분석 전용). 읽기만 한다.

화면은 목록 날 **이전**의 순위표만 쓴다(나중에 안 값으로 지난 화면을 바꾸지 않게). 순위표가
`MAX_RANK_AGE_SESSIONS`보다 오래됐으면 판정하지 않는다. 사후 분석(`list-review`)은 이전 표가 없으면 그날 또는 그 뒤
가장 가까운 표로 떼고 "사후 판정"이라고 표시한다.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.calendar import Market, MarketCalendar
from app.models import Instrument
from app.repositories import market_cap_repo
from app.scoring.heavyweight import MAX_RANK_AGE_SESSIONS, is_heavyweight

KR = MarketCalendar(Market.KR)


@dataclass(frozen=True, slots=True)
class Weight:
    weight_pct: float
    listing: str
    heavyweight: bool
    sector: str | None
    rank_day: date
    after_the_fact: bool = False


def weights_for(
    session: Session, day: date, instrument_ids: Collection[int], *, allow_after: bool = False
) -> dict[int, Weight]:
    rank_day = market_cap_repo.latest_day(session, before=day)
    after = False
    if rank_day is not None and len(KR.sessions_between(rank_day, day)) - 1 > MAX_RANK_AGE_SESSIONS:
        rank_day = None
    if rank_day is None and allow_after:
        rank_day = market_cap_repo.latest_day(session, on_or_after=day)
        after = rank_day is not None
    if rank_day is None:
        return {}
    found = market_cap_repo.weights_on(session, rank_day, instrument_ids)
    rows = session.execute(
        select(Instrument.instrument_id, Instrument.sector).where(
            Instrument.instrument_id.in_(list(found))
        )
    ).all()
    sectors: dict[int, str | None] = {row[0]: row[1] for row in rows}
    return {
        i: Weight(w, listing.value, is_heavyweight(w), sectors.get(i), rank_day, after)
        for i, (w, listing) in found.items()
    }
