"""증권사 투자의견(KIS 종목투자의견). 화면 참고 표시와 사후 기록 전용 — 목록 선정·채점에는 쓰지 않는다.

한 행은 KIS가 준 리포트 하나다(날짜·증권사·의견 원문·직전 의견·목표주가). 의견 원문은 증권사마다 표기가 달라(BUY, 매수,
Buy, Strong BUY, Outperform) 그대로 두고 읽을 때 정규화한다(`app/scoring/analyst.py`). 날짜는 시각이 없어서 목록 날 D에는
D보다 앞선 날짜만 쓴다.

`AnalystOpinionFetch`는 종목별 조회 기록이다. "리포트가 없다"와 "조회하지 못했다"를 가르는 유일한 근거라서 따로 둔다.
KIS는 한 번에 최신순 100행까지만 주고 연속 조회가 없다(2026-09-30 실측). 100행이 차고 가장 오래된 행이 조회 시작일보다
늦으면 잘린 것으로 적는다.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, IngestedAt
from app.models.market import Price

# 조회 기록 상태.
FETCH_OK = "OK"
FETCH_TRUNCATED = "TRUNCATED"
FETCH_FAILED = "FAILED"


class AnalystOpinion(Base):
    """리포트 하나의 투자의견과 목표주가(KIS 원문 그대로)."""

    __tablename__ = "analyst_opinion"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False, doc="KIS stck_bsop_date.")
    broker: Mapped[str] = mapped_column(String(40), nullable=False, doc="KIS mbcr_name.")
    opinion: Mapped[str] = mapped_column(String(40), nullable=False, doc="의견 원문(invt_opnn).")
    opinion_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    prior_opinion: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(
        Price, nullable=True, doc="목표주가. 0이나 빈 값은 NULL(Not Rated 등)."
    )
    prev_close: Mapped[Decimal | None] = mapped_column(Price, nullable=True)
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        # 같은 날 같은 증권사라도 의견·목표가가 다르면 둘 다 남긴다. 목표가 NULL끼리는 같은 것으로 본다.
        UniqueConstraint(
            "instrument_id",
            "report_date",
            "broker",
            "opinion",
            "target_price",
            name="uq_analyst_opinion_report",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_analyst_opinion_instrument_day", "instrument_id", "report_date"),
    )


class AnalystOpinionFetch(Base):
    """한 종목을 한 번 조회한 기록: 기간·상태·받은 행 수."""

    __tablename__ = "analyst_opinion_fetch"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, doc="OK, TRUNCATED, FAILED.")
    rows: Mapped[int] = mapped_column(Integer, nullable=False)
    oldest: Mapped[date | None] = mapped_column(
        Date, nullable=True, doc="받은 행 중 가장 이른 날짜(잘렸을 때 어디까지 믿을 수 있는지)."
    )

    __table_args__ = (Index("ix_analyst_opinion_fetch_instrument", "instrument_id", "end_date"),)
