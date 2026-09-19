"""Portfolio state: current holdings, daily snapshots and manual transactions.

`Holding` is the live mirror of the brokerage account. `PortfolioSnapshot` is
the append-only daily record that the equity curve is drawn from — without it
the return history would only ever be as long as the broker's own retention.
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

Money = Numeric(24, 6)
Qty = Numeric(24, 8)


class TransactionSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Holding(Base):
    """Current position in one instrument, synced from the broker.

    `avg_price` and `avg_fx_rate` are both kept so that unrealised profit can be
    split into the part that came from the stock and the part that came from the
    exchange rate. Without the second number a US position's KRW return is
    uninterpretable.
    """

    __tablename__ = "holding"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )

    quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(
        Money, nullable=False, doc="Average cost per share in the instrument's local currency"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KRW")
    avg_fx_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8),
        nullable=True,
        doc="Average local->KRW rate at acquisition. NULL for KRW instruments. "
        "Needed to separate stock return from currency return.",
    )

    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        UniqueConstraint("instrument_id", name="uq_holding_instrument"),
        Index("ix_holding_instrument", "instrument_id"),
    )

    def __repr__(self) -> str:
        return f"<Holding {self.instrument_id} qty={self.quantity}>"


class PortfolioSnapshot(Base):
    """End-of-day portfolio valuation. One row per day, append only."""

    __tablename__ = "portfolio_snapshot"

    id: Mapped[BigIntPk]
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)

    total_value_krw: Mapped[Decimal] = mapped_column(Money, nullable=False)
    invested_krw: Mapped[Decimal] = mapped_column(Money, nullable=False)
    cash_krw: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    pnl_krw: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)

    # The same profit decomposed, so the equity curve can be explained rather
    # than merely plotted.
    pnl_local_krw_equiv: Mapped[Decimal | None] = mapped_column(
        Money, nullable=True, doc="Portion of PnL attributable to price moves"
    )
    pnl_fx_krw: Mapped[Decimal | None] = mapped_column(
        Money, nullable=True, doc="Portion of PnL attributable to FX moves"
    )

    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        UniqueConstraint("snapshot_date", name="uq_portfolio_snapshot_date"),
        Index("ix_portfolio_snapshot_date", "snapshot_date"),
    )

    def __repr__(self) -> str:
        return f"<PortfolioSnapshot {self.snapshot_date} {self.total_value_krw}>"


class Transaction(Base):
    """A manually recorded trade.

    This system never places orders. Transactions are entered by the user so
    that cost basis and realised return survive independently of whatever the
    broker chooses to keep.
    """

    __tablename__ = "transaction"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )

    side: Mapped[TransactionSide] = mapped_column(
        Enum(TransactionSide, name="transaction_side", native_enum=False, length=8), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Qty, nullable=False)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="KRW")
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    fee: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    tax: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)

    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (Index("ix_transaction_instrument_time", "instrument_id", "executed_at"),)

    def __repr__(self) -> str:
        return f"<Transaction {self.side} {self.instrument_id} x{self.quantity}>"
