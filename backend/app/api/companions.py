"""장 초반 동반주 API: 그날 마지막 수집 시각의 이끄는 종목과 동반 후보. 읽기만 한다.

표시 전용이다(`app/services/companion_service.py`). 실시간 피드가 꺼져 있어도 보이게 DB에서 읽는다.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import companion_service

router = APIRouter(prefix="/api", tags=["companions"])

SessionDep = Annotated[Session, Depends(get_db)]


@router.get("/companions/today")
def companions_today(session: SessionDep, day: date | None = None) -> dict[str, Any]:
    return companion_service.today_view(session, day)
