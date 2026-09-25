"""One-minute bars from KIS, the record the intraday analysis is computed from.

**The REST bars fetched after the close are the record.** The live chart shows
what the WebSocket delivers during the session, and none of that is stored:
a dropped connection or a day nobody opened the chart must not leave a hole
in what is analysed.

**Times.** `ts` is the start of the minute, in UTC, and `available_at` its end:
the bar is knowable only once the minute is over — the same rule as a daily
bar, whose `available_at` is the session close. KIS labels a bar with the
minute it starts (seen on 2026-09-23: a `090000` bar exists and the last
continuous-trading bar is `151900`). The closing auction is the one `153000`
bar; 15:20 to 15:29 have none. A minute without a trade has no bar.

**Index bars** cannot be fetched for past days: the provider returns only the
latest session's last hundred or so minutes. They are collected during the
session, in pieces, and a day is complete only if every minute arrived.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, IngestedAt
from app.models.market import Price


class MinuteBar(Base):
    """One minute of one stock, as KIS reported it after the session."""

    __tablename__ = "minute_bar"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, doc="Start of the minute, UTC."
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, doc="End of the minute: when it was knowable."
    )
    open: Mapped[Decimal] = mapped_column(Price, nullable=False)
    high: Mapped[Decimal] = mapped_column(Price, nullable=False)
    low: Mapped[Decimal] = mapped_column(Price, nullable=False)
    close: Mapped[Decimal] = mapped_column(Price, nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="KIS")
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        UniqueConstraint("instrument_id", "ts", name="uq_minute_bar_instrument_ts"),
        Index("ix_minute_bar_instrument_day", "instrument_id", "session_date"),
    )


class IndexMinuteBar(Base):
    """One minute of a market index, collected during the session."""

    __tablename__ = "index_minute_bar"

    id: Mapped[BigIntPk]
    index_code: Mapped[str] = mapped_column(
        String(16), nullable=False, doc="The yfinance code the daily bars use: ^KS11, ^KQ11."
    )
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Price, nullable=False)
    high: Mapped[Decimal] = mapped_column(Price, nullable=False)
    low: Mapped[Decimal] = mapped_column(Price, nullable=False)
    close: Mapped[Decimal] = mapped_column(Price, nullable=False)
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        UniqueConstraint("index_code", "ts", name="uq_index_minute_bar_code_ts"),
        Index("ix_index_minute_bar_day", "index_code", "session_date"),
    )


class MinuteFetch(Base):
    """Whether a day's minute bars for one stock came in whole. Appended per attempt.

    The analysis of a day runs only on a COMPLETE fetch: one that walked back
    to 09:00, or past the start of the day. A fetch that stopped short is
    PARTIAL and its day is not analysed rather than analysed on half a day.
    EMPTY is a day the provider has no bars for (a suspension, a listing
    later than the day).
    """

    __tablename__ = "minute_fetch"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    bars: Mapped[int] = mapped_column(Integer, nullable=False)
    pages: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    __table_args__ = (Index("ix_minute_fetch_day", "instrument_id", "session_date", "fetched_at"),)


class IntradaySummary(Base):
    """What one stock did through one session, derived from its minute bars.

    Derived and versioned: recomputable from `minute_bar` at any time, so a
    change to how it is measured is a new `analysis_version`, not an edit. A
    day whose bars did not come in whole is recorded with its status and no
    measures — it is not analysed on half a day — and is replaced by the full
    analysis once the day arrives.

    MFE and MAE are hindsight: the day's best and worst price against the
    open, known only after the day. They say how much room there was, not a
    return anyone could have taken.
    """

    __tablename__ = "intraday_summary"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    analysis_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, doc="COMPLETE, or PARTIAL / EMPTY with no measures."
    )
    bars: Mapped[int] = mapped_column(Integer, nullable=False)
    return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    high_at: Mapped[str | None] = mapped_column(String(5), nullable=True, doc="HH:MM, Seoul.")
    low_at: Mapped[str | None] = mapped_column(String(5), nullable=True)
    minutes_to_high: Mapped[int | None] = mapped_column(Integer, nullable=True)
    close_vs_vwap_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    volatility_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_volume_at: Mapped[str | None] = mapped_column(String(5), nullable=True)
    first_hour_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="Open to the last close before 10:00."
    )
    index_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    market_return_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="The stock's index over the same session, open to close."
    )
    market_source: Mapped[str | None] = mapped_column(
        String(12),
        nullable=True,
        doc="INDEX_MINUTE when the index's minutes were whole, else DAILY.",
    )
    buckets: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, doc="Thirteen half-hours: return, volume share, bars, index return."
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "session_date", "analysis_version", name="uq_intraday_summary_day"
        ),
    )
