"""Collector execution history.

This project talks to roughly ten external sources. When a score looks wrong the
first question is always "which input was missing?", and without a run log that
question takes an afternoon instead of one query.

The status set matters more than it looks:

    SUCCESS   ran, got what it expected
    PARTIAL   ran, got some of it (rate limited, a page failed)
    FAILED    ran, broke                      <- an outage; worth alarming on
    SKIPPED   did not run: no credentials     <- a configuration gap, not an outage

`SKIPPED` exists because a missing API key and a broken API are different
events. Treating them alike means a system with no keys configured looks like a
system on fire, and the dashboard can no longer tell the user the one useful
thing: which value to go and fill in.

Crucially, a failed run does **not** by itself make a factor unusable. That
decision runs through freshness (see `app.scoring.availability`): a DART
collector that failed this morning has no bearing on a quarterly filing
collected last week.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk


class CollectorStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class CollectorRun(Base):
    """One execution of one collector."""

    __tablename__ = "collector_run"

    id: Mapped[BigIntPk]
    source: Mapped[str] = mapped_column(
        String(48), nullable=False, doc="Collector name, e.g. SEC_EDGAR, NAVER_NEWS"
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[CollectorStatus] = mapped_column(
        Enum(CollectorStatus, name="collector_status", native_enum=False, length=12), nullable=False
    )

    items_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_saved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        doc="Human-readable note. For SKIPPED this says which env var to set.",
    )

    __table_args__ = (
        Index("ix_collector_run_source_started", "source", "started_at"),
        # Supports "when did this source last succeed?", which is the question
        # fundamental freshness is actually asking.
        Index("ix_collector_run_source_status", "source", "status", "finished_at"),
    )

    def __repr__(self) -> str:
        return f"<CollectorRun {self.source} {self.status} read={self.items_read}>"
