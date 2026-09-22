"""The call ledger: what we have already spent against each provider.

One row per (quota group, endpoint, minute). Windows are answered by summing
buckets, which is what lets a single table serve a rolling 24 hours, a rolling
31 days and a rolling 10 minutes without any of them being a different shape.

**Summing is always by `quota_group`.** Naver's 25,000 calls a day are metered
against the client ID across its whole search family, so counting news and blog
separately would let each spend the full cap. `endpoint` exists to answer "what
ate the budget" after the fact and never enters a window predicate.

**Minute buckets, not one row per call.** The row count is bounded by elapsed
minutes rather than by traffic, so a runaway loop cannot also run away with the
table. The cost is resolution: a window floored to the minute takes the boundary
minute whole and therefore counts a few calls that have already aged out. That
is an overestimate, which is the only direction a never-exceed budget may err in.

There is no `ingested_at` here. This is operational accounting, not market data:
there is no valid-time separate from transaction-time, and a second timestamp
would only raise the question of which one a window uses.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk


class ApiCallBucket(Base):
    """Calls made against one quota group during one minute."""

    __tablename__ = "api_call_bucket"

    id: Mapped[BigIntPk]

    quota_group: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        doc="The accounting unit: naver_search, naver_datalab, threads, reddit, dart. "
        "Windows are summed over this, never over endpoint.",
    )
    endpoint: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="Which call within the group — news, company, list. Diagnostic only: "
        "it splits rows so spending can be attributed, and is never a window predicate.",
    )
    minute_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="UTC minute this call fell in, truncated. UTC rather than a local "
        "minute so a DST transition cannot make two buckets uncomparable.",
    )
    calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Calls reserved in this minute. Reserved, not completed — a request "
        "counts against the provider whether or not we liked the answer.",
    )

    __table_args__ = (
        UniqueConstraint("quota_group", "endpoint", "minute_start", name="uq_api_call_bucket_slot"),
        Index("ix_api_call_bucket_window", "quota_group", "minute_start"),
    )

    def __repr__(self) -> str:
        return (
            f"<ApiCallBucket {self.quota_group}/{self.endpoint} "
            f"{self.minute_start} calls={self.calls}>"
        )
