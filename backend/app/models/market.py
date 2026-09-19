"""Market data: candles, FX rates and corporate actions.

Candles are stored **raw**. Split- and dividend-adjusted prices are derived at
read time from `corporate_action`, never written over the original bars. Once
you overwrite a bar with an adjusted price the original is unrecoverable, and
every future adjustment compounds the error.

FX carries the bitemporal pair like every other source table, because a
portfolio valuation or a backtest reproduced later must see the rates as they
stood, not as they were subsequently corrected.
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


class Interval(StrEnum):
    """Bar sizes. Toss publishes 1-minute and daily; longer bars are derived."""

    MIN_1 = "1m"
    DAY_1 = "1d"


class CorporateActionType(StrEnum):
    DIVIDEND = "DIVIDEND"
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    BONUS_ISSUE = "BONUS_ISSUE"
    MERGER = "MERGER"


# Prices: 20 digits with 6 decimal places. Numeric rather than float because
# money that silently loses precision is worse than money that is slow.
Price = Numeric(20, 6)


class Candle(Base):
    """One raw OHLCV bar, as reported at a point in time.

    **Revisions are kept, never overwritten.** A provider that restates a past
    bar produces a new row rather than mutating the old one.

    The alternative is worse than it looks. Updating OHLCV in place while
    holding `ingested_at` at its original value yields a row whose *values*
    arrived later but whose *transaction time* claims they were always there.
    A reproduce-mode query filtering `ingested_at <= data_snapshot_at` would
    then admit a correction into a snapshot that predates it — which is the
    exact failure this schema exists to prevent. Same reasoning as
    `fundamental`, which keeps every filed revision.

    So (instrument_id, interval, ts) is deliberately **not** unique. Reads take
    the newest revision; point-in-time reads take the newest revision whose
    `ingested_at` is within the snapshot.
    """

    __tablename__ = "candle"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    interval: Mapped[Interval] = mapped_column(
        Enum(Interval, name="candle_interval", native_enum=False, length=8), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="Bar open instant, UTC. For daily bars this is the session open.",
    )

    open: Mapped[Decimal] = mapped_column(Price, nullable=False)
    high: Mapped[Decimal] = mapped_column(Price, nullable=False)
    low: Mapped[Decimal] = mapped_column(Price, nullable=False)
    close: Mapped[Decimal] = mapped_column(Price, nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False, default=0)

    source: Mapped[str] = mapped_column(String(24), nullable=False, default="TOSS")
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        # Not unique on (instrument_id, interval, ts): a restated bar is a new
        # revision, distinguished by ingested_at.
        Index("ix_candle_lookup", "instrument_id", "interval", "ts", "ingested_at"),
        Index("ix_candle_ingested", "ingested_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Candle {self.instrument_id} {self.interval} {self.ts} "
            f"c={self.close} ingested={self.ingested_at}>"
        )


class FxRate(Base):
    """A daily FX rate, used to value non-KRW holdings and split return sources.

    Return is reported both in KRW and in local currency so that a gain from a
    rising stock is never confused with a gain from a rising dollar.
    """

    __tablename__ = "fx_rate"

    id: Mapped[BigIntPk]
    base: Mapped[str] = mapped_column(String(3), nullable=False, doc="e.g. USD")
    quote: Mapped[str] = mapped_column(String(3), nullable=False, doc="e.g. KRW")
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)

    source: Mapped[str] = mapped_column(String(24), nullable=False, default="YFINANCE")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="Valid time: when a market participant could have used this rate.",
    )
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        UniqueConstraint("base", "quote", "rate_date", name="uq_fx_rate_base_quote_date"),
        Index("ix_fx_rate_pair_date", "base", "quote", "rate_date"),
    )

    def __repr__(self) -> str:
        return f"<FxRate {self.base}/{self.quote} {self.rate_date} {self.rate}>"


class CorporateAction(Base):
    """A dividend, split, merger or bonus issue.

    `announced_at` and `ex_date` are different clocks and both matter: the
    announcement is when the market learned, the ex-date is when the price
    mechanically changes. `available_at` follows the same next-session rule used
    for filings, since announcement dates arrive at date granularity too.
    """

    __tablename__ = "corporate_action"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[CorporateActionType] = mapped_column(
        Enum(CorporateActionType, name="corporate_action_type", native_enum=False, length=20),
        nullable=False,
    )

    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    announced_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[IngestedAt]

    ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True, doc="Split ratio, e.g. 4.0 for a 4-for-1 split"
    )
    amount: Mapped[Decimal | None] = mapped_column(
        Price, nullable=True, doc="Cash dividend per share, in the local currency"
    )
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False, default="YFINANCE")

    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "action_type",
            "ex_date",
            name="uq_corporate_action_instrument_type_exdate",
        ),
        Index("ix_corporate_action_lookup", "instrument_id", "ex_date"),
    )

    def __repr__(self) -> str:
        return f"<CorporateAction {self.instrument_id} {self.action_type} ex={self.ex_date}>"
