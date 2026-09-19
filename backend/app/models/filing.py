"""Filing metadata — the register of what was submitted, and when.

Separate from `fundamental`, and necessary because the two answer different
questions. `fundamental` holds tagged values; this holds the existence of
reports, including ones whose values we cannot read.

That gap is real and large. SEC's XBRL tagging was phased in from June 2009, so
`companyfacts` carries no values from earlier filings — Apple's earliest tagged
fact is dated 2009-07-22. But `submissions` lists every filing back to the
1990s, including:

    filed 2008-11-05   10-K   period_of_report 2008-09-27

which is the annual report where Apple's FY2008 EPS was actually first
published. Without this table, a lookup at 2009-08-01 finds no tagged value and
has no way to tell whether the report existed. With it, the system can say the
true thing: the report was filed, the market had the figure, and our value
source simply does not reach back that far.

This is what lets `NOT_YET_FILED` be a claim rather than a guess. It is only
returned when the filing register positively shows no qualifying report had
been submitted.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, IngestedAt
from app.models.fundamental import FundamentalSource


class Filing(Base):
    """One submitted report."""

    __tablename__ = "filing"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )

    form: Mapped[str] = mapped_column(
        String(24), nullable=False, doc="10-K, 10-K/A, 10-Q, 사업보고서 …"
    )
    filed_at: Mapped[date] = mapped_column(Date, nullable=False)
    period_of_report: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        doc="The period the report covers. Matching this against a fiscal "
        "period is what proves whether a figure had been published.",
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="Next market open after filed_at, same rule as `fundamental`.",
    )
    accession: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[FundamentalSource] = mapped_column(
        Enum(FundamentalSource, name="filing_source", native_enum=False, length=12),
        nullable=False,
    )
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        UniqueConstraint("instrument_id", "accession", name="uq_filing_instrument_accession"),
        Index("ix_filing_lookup", "instrument_id", "form", "period_of_report", "filed_at"),
        Index("ix_filing_available", "instrument_id", "available_at"),
    )

    def __repr__(self) -> str:
        return f"<Filing {self.form} filed={self.filed_at} period={self.period_of_report}>"
