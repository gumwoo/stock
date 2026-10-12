"""장 시작 직후 동반주 기록(2026-10-12부터). 표시·기록 전용이고, 쓴 행은 고치지 않는다(다시 돌면 충돌한 행은 건너뛴다).

- `open_mover`: 거래일 개장 1~10분 매분 KIS 등락률 순위(시장별 상위 30, 종가 대비)에서 마스터에 있는 보통주.
- `companion_alert`: 그 분의 이끄는 종목(+7% 이상)과 동반 후보, 후보의 그때 등락률과 실제 조회 시각.
- `companion_ask`: 밤에 Claude에게 관련주를 물은 기록(질문 묶음·원 응답). 같은 종목은 30일 안에 다시 묻지 않는다.
- `companion_relation`: 그 응답에서 마스터 이름과 정확히 하나가 맞은 관계. 이름 묶음 관계는 저장하지 않고 그때마다 마스터에서 계산한다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, IngestedAt


class OpenMover(Base):
    __tablename__ = "open_mover"

    id: Mapped[BigIntPk]
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="이 실행이 시작된 시각(한 실행의 행은 같은 값).",
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, doc="이 행의 순위를 받은 시각."
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    market_rank: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="시장(KOSPI/KOSDAQ) 안 순위."
    )
    change_pct: Mapped[float] = mapped_column(Float, nullable=False, doc="prdy_ctrt(전일 대비 %).")
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_change_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="oprc_vrss_prpr_rate(시가 대비 %)."
    )
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (UniqueConstraint("at", "instrument_id", name="uq_open_mover_at"),)


class CompanionAlert(Base):
    __tablename__ = "companion_alert"

    id: Mapped[BigIntPk]
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    leader_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    leader_change: Mapped[float] = mapped_column(Float, nullable=False)
    companion_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    companion_change: Mapped[float | None] = mapped_column(Float, nullable=True)
    companion_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    companion_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="동반 후보 등락률을 받은 시각(순위나 현재가)."
    )
    sources: Mapped[list[str]] = mapped_column(JSON, nullable=False, doc="NAME / AI.")
    reason: Mapped[str | None] = mapped_column(
        String(200), nullable=True, doc="AI가 쓴 이유(추정)."
    )
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        UniqueConstraint("at", "leader_id", "companion_id", name="uq_companion_alert_at"),
    )


class CompanionAsk(Base):
    __tablename__ = "companion_ask"

    id: Mapped[BigIntPk]
    asked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    batch: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, doc="물은 종목들 [{n, instrument_id, name}]."
    )
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, doc="원 응답.")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[IngestedAt]


class CompanionRelation(Base):
    __tablename__ = "companion_relation"

    id: Mapped[BigIntPk]
    ask_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companion_ask.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    related_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    in_batch: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        doc="같은 질문 묶음에 있던 종목을 골랐나(편향 — 이름 규칙과 맞을 때만 보인다).",
    )
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        UniqueConstraint("ask_id", "instrument_id", "related_id", name="uq_companion_relation"),
    )
