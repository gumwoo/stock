"""When a name found in the news became one we follow, and on what evidence.

`instrument.tracked` says what is followed now. It cannot say since when, so a
question about a past moment — which names were already tracked, and so not
candidates, at 09/23 10:00 — would be answered with today's list. A promotion
row closes that gap: a name promoted after a moment was untracked at it.

It is also the audit trail for the selection bias the README warns about. A
name picked because the news surged on it carries the numbers that picked it,
and a backtest that later includes it can say so.

Written once, never updated. `promoted_at` is stamped by the database.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk


class InstrumentPromotion(Base):
    """One untracked name made tracked, with the surge that put it forward."""

    __tablename__ = "instrument_promotion"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    promoted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        nullable=False,
        doc="When the name became tracked. Set by the database.",
    )
    discovered_asof: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="The moment the discovery that put it forward was computed for.",
    )
    window_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_days: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        doc="Baseline actually covered by collection, not the nominal length.",
    )
    recent_mentions: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_mentions: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    news_freshness: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        doc="FRESH, STALE or MISSING: whether news was flowing when it was picked.",
    )
    candle_bars: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="Daily bars stored when it was promoted."
    )
    fundamental_facts: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="Financial facts stored when it was promoted."
    )

    __table_args__ = (Index("ix_instrument_promotion_instrument", "instrument_id", "promoted_at"),)

    def __repr__(self) -> str:
        return (
            f"<InstrumentPromotion instrument={self.instrument_id} at {self.promoted_at} "
            f"score={self.score:.2f}>"
        )
