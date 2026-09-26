"""테마어 뉴스 API: 가장 최근 아침의 테마별 기사 수와 언급 종목. 읽기만 한다.

표시 전용이다(`app/models/theme.py`). 라이브 피드가 꺼져 있어도 보이게 게이트웨이가 아니라 DB에서 읽는다.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.theme import ThemeNewsDay

router = APIRouter(prefix="/api", tags=["themes"])

SessionDep = Annotated[Session, Depends(get_db)]


@router.get("/themes/today")
def themes_today(session: SessionDep) -> dict[str, Any]:
    day = session.scalar(select(func.max(ThemeNewsDay.session_date)))
    if day is None:
        return {"day": None, "themes": []}
    rows = session.scalars(
        select(ThemeNewsDay)
        .where(ThemeNewsDay.session_date == day)
        .order_by(ThemeNewsDay.articles.desc(), ThemeNewsDay.theme)
    ).all()
    return {
        "day": day.isoformat(),
        "themes": [
            {
                "theme": r.theme,
                "query": r.query,
                "since": r.since.isoformat(),
                "asked_at": r.asked_at.isoformat(),
                "articles": r.articles,
                "capped": r.capped,
                "headlines": r.headlines,
                "mentions": r.mentions,
            }
            for r in rows
        ],
    }
