"""전략 실험실 화면(연구 탭): 매일 쌓이는 아침 목록 기록과 3개월 기준표로 규칙·종목 조건 비교, 고정 가설 추적. 읽기만 한다."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import lab_service

router = APIRouter(prefix="/api/lab", tags=["lab"])
SessionDep = Annotated[Session, Depends(get_db)]


@router.get("")
def lab(session: SessionDep) -> dict[str, Any]:
    return lab_service.lab_view(session)
