"""The morning's watchlist, frozen as it was chosen.

Written once and never updated. Each member carries the numbers that put it
there, as they stood at the snapshot's moment — not a pointer to rows that a
later re-judgement or a new model could change. "Why did we look at this
name that morning" must have the same answer next year as today.

The snapshot also records what the morning's inputs were in: whether each
member's news had been swept since the morning, whether the model's reading
ran, whether the search trends arrived. A snapshot is taken even when one of
them failed, and says so, because the day's outcome is to be read in the
light of what the choice was made without.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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

from app.models.base import Base, BigIntPk


class WatchlistSnapshot(Base):
    """One morning's choice: when, under which versions, and with what missing."""

    __tablename__ = "watchlist_snapshot"

    id: Mapped[BigIntPk]
    session_date: Mapped[date] = mapped_column(
        Date, nullable=False, doc="The trading day the list is for."
    )
    asof: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, doc="The moment everything was read as of."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    selection_version: Mapped[int] = mapped_column(Integer, nullable=False)
    versions: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        doc="Overlay, relevance rule, reading model and prompts, attention, regime.",
    )
    inputs: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        doc="What the morning's inputs were in: news coverage, the model's run, search trends.",
    )
    pool: Mapped[int] = mapped_column(Integer, nullable=False, doc="Names considered.")
    left_out: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="Names with a reason that did not fit."
    )

    __table_args__ = (
        Index("ix_watchlist_snapshot_day", "session_date", "created_at"),
        # One morning, one list: the database refuses a second, whoever takes it.
        UniqueConstraint("session_date", "strategy_version", name="uq_watchlist_snapshot_day"),
    )


class WatchlistMember(Base):
    """One name on a morning's list, with the numbers that put it there."""

    __tablename__ = "watchlist_member"

    id: Mapped[BigIntPk]
    snapshot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("watchlist_snapshot.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    tracked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    overlay_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    overlay_events: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, doc="The overlay's clusters at the moment, largest first."
    )
    attention_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    attention_surge: Mapped[float | None] = mapped_column(Float, nullable=True)
    discovery_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    regime: Mapped[str | None] = mapped_column(String(12), nullable=True)
    signal_decision_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="The close whose signal the scores below are from. None for an untracked name.",
    )
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    technical_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    fundamental_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    news_swept_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="The newest sweep of this name's news recorded by the moment.",
    )

    __table_args__ = (
        UniqueConstraint("snapshot_id", "instrument_id", name="uq_watchlist_member_name"),
        UniqueConstraint("snapshot_id", "rank", name="uq_watchlist_member_rank"),
    )
