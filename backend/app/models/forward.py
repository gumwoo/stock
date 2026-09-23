"""What happened after a judgement: the forward test (Phase 4-8).

News has no past to backtest on, so whether the overlay, the relevance rule
or the discovery ranking mean anything can only be learned by writing down
what they said and, later, what the market did. These tables are that
record. They are written after the fact and never change what was judged:
a signal, its overlay and a candidate list are stored when made, and an
outcome is added once the prices it needs exist.

The fill follows the backtest's rule. A signal is judged at a close, so the
earliest honest entry is the next session's open (`earliest_execution_at`);
the exit is the close of the session `horizon_sessions` in, counting the
entry session as the first. A candidate list is entered the same way, from
the first open after the moment it was taken.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
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

from app.models.base import Base, BigIntPk

# Sessions from entry to exit. One day, one week, one month of trading.
HORIZONS: tuple[int, ...] = (1, 5, 20)


class SignalOutcome(Base):
    """A signal's return over one horizon, from its earliest honest fill."""

    __tablename__ = "signal_outcome"

    id: Mapped[BigIntPk]
    signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("signal.id", ondelete="CASCADE"), nullable=False
    )
    horizon_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, doc="The exit session's close."
    )
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    return_pct: Mapped[float] = mapped_column(Float, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("signal_id", "horizon_sessions", name="uq_signal_outcome_horizon"),
    )


class CandidateSnapshot(Base):
    """One name on the discovery list as it stood when the list was taken."""

    __tablename__ = "candidate_snapshot"

    id: Mapped[BigIntPk]
    taken_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    asof: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    recent_mentions: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_mentions: Mapped[int] = mapped_column(Integer, nullable=False)
    recent_days: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_days: Mapped[float] = mapped_column(Float, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    news_freshness: Mapped[str] = mapped_column(String(8), nullable=False)

    __table_args__ = (
        UniqueConstraint("asof", "instrument_id", name="uq_candidate_snapshot_asof_instrument"),
        Index("ix_candidate_snapshot_asof", "asof"),
    )


class CandidateOutcome(Base):
    """A listed candidate's return over one horizon from the next open."""

    __tablename__ = "candidate_outcome"

    id: Mapped[BigIntPk]
    snapshot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("candidate_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    horizon_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    return_pct: Mapped[float] = mapped_column(Float, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("snapshot_id", "horizon_sessions", name="uq_candidate_outcome_horizon"),
    )
