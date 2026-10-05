"""9시 전 예상체결가(KIS 주식현재가 호가/예상체결, TR FHKST01010200) 조회 기록.

평일 08:50(판정)과 08:57(기록만)에 그날 아침 목록 종목의 장전 동시호가 예상체결가를 받는다. 08:50 판정에서 예상 시가가
전일 대비 +3% 이상인 종목은 목록 행의 `excluded_reason`에 GAP_UP이 붙는다(얼린 행을 고치는 유일한 경우 — 판단의 원본은
이 표다). 08:57 기록은 판정을 바꾸지 않고, 09:00에 더 가까운 값이 얼마나 달라지는지 나중에 재려고 남긴다.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, BigInteger, Date, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, IngestedAt
from app.models.market import Price


class ExpectedOpen(Base):
    """한 종목의 한 번 조회. 판정할 수 없는 값(0, 불일치)도 그대로 남긴다."""

    __tablename__ = "expected_open"

    id: Mapped[BigIntPk]
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="조회 실행이 시작된 시각(한 실행의 행은 같은 값).",
    )
    purpose: Mapped[str] = mapped_column(
        String(8), nullable=False, doc="decide(08:50) / record(08:57)."
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_price: Mapped[Decimal | None] = mapped_column(Price, nullable=True, doc="antc_cnpr.")
    base_price: Mapped[Decimal | None] = mapped_column(
        Price, nullable=True, doc="stck_sdpr(기준가)."
    )
    change_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="expected_price / base_price - 1 (%), 직접 계산."
    )
    reported_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="antc_cntg_prdy_ctrt(KIS가 준 전일 대비율)."
    )
    expected_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True, doc="antc_vol.")
    mkop_code: Mapped[str | None] = mapped_column(
        String(8), nullable=True, doc="antc_mkop_cls_code."
    )
    judgeable: Mapped[bool] = mapped_column(nullable=False, default=False)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, doc="output2 원본.")
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (UniqueConstraint("instrument_id", "check_at", name="uq_expected_open_check"),)
