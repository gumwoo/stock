"""Search attention: how much Naver users searched for a company's stock.

From Naver DataLab's search trend (via NAVER API Hub). A trend is relative,
not a count: each request scales its own series so that its busiest day is
100. So a stored series means something only against itself — a surge is the
recent level over the earlier level of the *same* fetch, which the scaling
cancels out of. Two fetches are never compared value for value.

One name per request, for the same reason: batched with Samsung Electronics,
a small company's series came back as 0.0004 at five decimal places, which is
rounding, not measurement.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, IngestedAt


class SearchTrend(Base):
    """One fetch of one company's daily search trend, as the provider scaled it.

    Kept whole rather than as points, because the points of one fetch share a
    scale and those of another do not. A fetch with no points means the
    company was searched too little to measure, which is an answer, and is
    why the fetch is stored even then.
    """

    __tablename__ = "search_trend"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        nullable=False,
        doc="Stamped by the database when stored: the moment this series was known.",
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    series: Mapped[dict[str, float]] = mapped_column(
        JSON,
        nullable=False,
        doc="ISO day to ratio. A day the provider left out had too few searches to measure.",
    )

    __table_args__ = (Index("ix_search_trend_instrument_fetched", "instrument_id", "fetched_at"),)


class SignalAttention(Base):
    """The search attention on a company when a signal about it was made, beside the signal.

    Like the overlay and the regime: recorded, not scored. Whether a surge of
    searches before a judgement says anything about the judgement is what the
    forward record is for.
    """

    __tablename__ = "signal_attention"

    id: Mapped[BigIntPk]
    signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("signal.id", ondelete="CASCADE"), nullable=False
    )
    asof: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attention_version: Mapped[int] = mapped_column(Integer, nullable=False)
    trend_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("search_trend.id", ondelete="SET NULL"),
        nullable=True,
        doc="The fetch read. Null when none was stored by the moment.",
    )
    surge: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        doc="MEASURED, UNMEASURED (too few searches), STALE (fetch behind) or NO_FETCH.",
    )
    created_at: Mapped[IngestedAt]

    __table_args__ = (UniqueConstraint("signal_id", name="uq_signal_attention_signal"),)
