"""Every call to a language model, with what it cost and how full the account was.

The quota ledger (`api_call_bucket`) counts calls to keep under a ceiling; it
cannot say what they were for, how many tokens they took, or how much of the
subscription they left. This table can, one row per call, written whether the
call succeeded or not.

`notional_cost_usd` is what the API would have charged. Under the Claude
subscription nothing is billed per call; the number is kept so that a later
move to metered billing can be priced from history rather than guessed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk


class LlmCall(Base):
    __tablename__ = "llm_call"

    id: Mapped[BigIntPk]
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    purpose: Mapped[str] = mapped_column(
        String(32), nullable=False, doc="`relevance` or `sentiment`."
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    items: Mapped[int] = mapped_column(Integer, nullable=False, doc="Articles in the batch.")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, doc="OK, RATE_LIMITED, UNAVAILABLE or MALFORMED."
    )
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notional_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    five_hour_utilization: Mapped[float | None] = mapped_column(Float, nullable=True)
    seven_day_utilization: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_llm_call_purpose_time", "purpose", "called_at"),)
