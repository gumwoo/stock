"""밤사이 미국 반도체(화면 참고용). 읽기만 한다."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.db import get_db
from app.services import overnight_service

router = APIRouter(prefix="/api/overnight", tags=["overnight"])
SessionDep = Annotated[Session, Depends(get_db)]


@router.get("/us-semis")
def us_semis(session: SessionDep, day: date | None = None) -> dict[str, Any]:
    """한국 거래일 `day`(없으면 오늘, 휴장일이면 다음 세션) 개장 전에 끝난 미국 반도체 등락."""
    return overnight_service.us_semis(session, day or overnight_service.KR.local_today(utc_now()))
