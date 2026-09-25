"""Event disclosures filed with DART: buybacks, contracts, share issues, mergers.

The periodic reports in `filing` feed the financial statements. These are the
other kind — the filings a company must make when something happens — and
they are the most authoritative account of an event there is: an article
about a buyback is usually a retelling of the buyback disclosure.

A row is the fact of the filing and never changes. What kind of event it is
is decided by `app.scoring.disclosure_events` from the report name, in code
with a version, so a better rule reclassifies every past filing without a
migration and the overlay recorded at the time keeps what the rule said then.

**Available from the next session's open.** `list.json` gives the receipt date
and no time, so the earliest moment the market can be assumed to know is the
following open — the same rule the periodic filings follow.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, IngestedAt


class Disclosure(Base):
    __tablename__ = "disclosure"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    rcept_no: Mapped[str] = mapped_column(
        String(14), nullable=False, unique=True, doc="DART receipt number."
    )
    report_nm: Mapped[str] = mapped_column(String(300), nullable=False)
    pblntf_ty: Mapped[str] = mapped_column(
        String(2), nullable=False, doc="B 주요사항보고, I 거래소공시, ..."
    )
    filer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    filed_on: Mapped[date] = mapped_column(Date, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="The next session's open after `filed_on`: the receipt has a date, not a time.",
    )
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (Index("ix_disclosure_instrument_available", "instrument_id", "available_at"),)
