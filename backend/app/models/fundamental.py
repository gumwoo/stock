"""Financial statement facts, stored as filed.

The table that makes point-in-time fundamentals possible, and the reason it is
shaped this way is best shown by a real example. Apple's FY2008 basic EPS:

    filed 2009-10-27   5.48
    filed 2010-10-27   6.94

Same company, same fiscal year, same concept — a 27% difference, because Apple
adopted new revenue-recognition rules retrospectively in 2010. A backtest
running in mid-2010 must see 5.48. Storing one value per period would make that
unrecoverable, so every revision is kept and selection happens at read time.

**The selection key is not (concept, period).** SEC's companyfacts is shaped
`facts[taxonomy][concept]["units"][unit]`, and the same concept appears under
different units and different report contexts. `EarningsPerShareBasic` in
USD/shares and `Revenues` in USD are not comparable rows, and a 10-Q figure for
a quarter is not the same statement as a 10-K figure for a year. So the context
that must match before choosing a revision is:

    (taxonomy, concept, unit, period_start, period_end, form)

and only within that does `filed_at <= asof` pick a winner.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, IngestedAt


class FundamentalSource(StrEnum):
    """Where a fact came from, which determines how far it can be trusted.

    SEC carries a per-fact `filed` date, so its history is genuinely
    reconstructable. DART gives a filing date at day granularity, which is
    enough. YFINANCE exposes only the latest restated figures with no filing
    date at all, so it is a gap-filler for the forward window and must never be
    used to reconstruct a past view.
    """

    SEC = "SEC"
    DART = "DART"
    YFINANCE = "YFINANCE"


class FiscalPeriod(StrEnum):
    """Which part of the fiscal year a fact covers."""

    FY = "FY"
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"
    H1 = "H1"
    UNKNOWN = "UNKNOWN"


class Fundamental(Base):
    """One reported financial fact, as it stood in one filing.

    Append-only. A restatement is a new row, never an update — the same rule
    `candle` follows, for the same reason: an in-place edit would leave the
    value and the timestamps disagreeing about when it was true.
    """

    __tablename__ = "fundamental"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )

    # --- what was measured ------------------------------------------------
    taxonomy: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        doc="us-gaap, dei, ifrs-full, srt — or 'dart' for Korean filings",
    )
    concept: Mapped[str] = mapped_column(
        String(128), nullable=False, doc="e.g. EarningsPerShareBasic, Revenues"
    )
    unit: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="USD, USD-per-shares, shares, pure, KRW. Not optional: without it "
        "an EPS and a revenue figure for the same concept name would collide.",
    )

    # --- the period it covers ---------------------------------------------
    period_start: Mapped[date | None] = mapped_column(
        Date, nullable=True, doc="NULL for instantaneous facts such as a balance"
    )
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_year: Mapped[int | None] = mapped_column(nullable=True)
    fiscal_period: Mapped[FiscalPeriod] = mapped_column(
        Enum(FiscalPeriod, name="fiscal_period", native_enum=False, length=8),
        nullable=False,
        default=FiscalPeriod.UNKNOWN,
    )
    form: Mapped[str] = mapped_column(
        String(24), nullable=False, doc="10-K, 10-Q, 20-F, 사업보고서 …"
    )

    # --- the value --------------------------------------------------------
    value: Mapped[Decimal] = mapped_column(Numeric(30, 6), nullable=False)

    # --- the three clocks -------------------------------------------------
    filed_at: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        doc="Date the filing was submitted. Day granularity is all either "
        "regulator gives, which is why available_at is a session boundary.",
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="Next market open after filed_at. A filing date cannot distinguish "
        "a 06:00 disclosure from a 14:00 one, and under Regulation S-T Rule 13 "
        "anything transmitted after 17:30 ET is deemed filed the next business "
        "day regardless — so the honest boundary is the following session.",
    )
    ingested_at: Mapped[IngestedAt]

    # --- provenance -------------------------------------------------------
    accession: Mapped[str | None] = mapped_column(
        String(32), nullable=True, doc="SEC accession number, or DART rcept_no"
    )
    source: Mapped[FundamentalSource] = mapped_column(
        Enum(FundamentalSource, name="fundamental_source", native_enum=False, length=12),
        nullable=False,
    )
    frame: Mapped[str | None] = mapped_column(
        String(32), nullable=True, doc="SEC calendar frame, e.g. CY2008Q4I"
    )

    __table_args__ = (
        # One row per (context, filing). A filing restating an earlier period
        # produces a different accession, hence a different row.
        #
        # NULLS NOT DISTINCT is essential, not a refinement. `period_start` is
        # NULL for instantaneous facts — a balance sheet figure is measured at a
        # date, not over a span — and Postgres's default treats every NULL as
        # distinct from every other. Without this, Assets, StockholdersEquity
        # and Cash would never conflict and would duplicate on every collection.
        UniqueConstraint(
            "instrument_id",
            "taxonomy",
            "concept",
            "unit",
            "period_start",
            "period_end",
            "form",
            "filed_at",
            "accession",
            name="uq_fundamental_context_filing",
            postgresql_nulls_not_distinct=True,
        ),
        # The point-in-time lookup: match the context, then filter on filing.
        Index(
            "ix_fundamental_pit",
            "instrument_id",
            "taxonomy",
            "concept",
            "unit",
            "period_end",
            "filed_at",
        ),
        Index("ix_fundamental_available", "instrument_id", "available_at"),
        Index("ix_fundamental_ingested", "ingested_at"),
    )

    def __repr__(self) -> str:
        return f"<Fundamental {self.concept} {self.period_end} ={self.value} filed={self.filed_at}>"
