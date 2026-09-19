"""Instrument identity.

The single most consequential schema decision in this project: **a ticker is not
an identity**. Tickers get reassigned, companies rename and merge (FB became
META, and the old ticker is reusable by someone else). Any table keyed on a
symbol string will quietly attribute one company's history to another once a
multi-year window is involved.

So `instrument_id` is an opaque, immutable surrogate key that everything else
references, and the symbol is demoted to a time-bounded attribute in
`symbol_history`.

For external anchors we use the identifiers that genuinely do not change:
SEC CIK for US issuers and the DART corp code for Korean ones.

A caveat worth stating in the schema rather than only in docs: **SEC does not
publish ticker change history.** `submissions` carries current `tickers` and
`exchanges` plus `formerNames`, and `formerNames` is former *company names*,
not former symbols. So `symbol_history` is populated from a separate historical
master, from corporate-action data, and from changes this system observes once
it starts running. Rows record their `source` so that mappings we merely
inferred are distinguishable from ones we actually observed.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import BigInteger, Date, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.calendar import Market
from app.models.base import Base, BigIntPk, IngestedAt


class Instrument(Base):
    """One tradable company, identified independently of its ticker."""

    __tablename__ = "instrument"

    instrument_id: Mapped[BigIntPk]

    market: Mapped[Market] = mapped_column(
        Enum(Market, name="market", native_enum=False, length=8),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Listing window. Required for point-in-time universe reconstruction: a
    # backtest over 2023-2026 that uses today's listed names silently drops
    # every company delisted in between and overstates returns.
    listed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    delisted_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Stable external anchors. These do not change when a ticker does.
    us_cik: Mapped[str | None] = mapped_column(
        String(10), nullable=True, doc="SEC Central Index Key, zero-padded to 10 digits"
    )
    kr_corp_code: Mapped[str | None] = mapped_column(
        String(8), nullable=True, doc="OpenDART corp_code (8 digits)"
    )

    ingested_at: Mapped[IngestedAt]

    symbols: Mapped[list[SymbolHistory]] = relationship(
        back_populates="instrument",
        cascade="all, delete-orphan",
        order_by="SymbolHistory.valid_from",
    )

    __table_args__ = (
        UniqueConstraint("us_cik", name="uq_instrument_us_cik"),
        UniqueConstraint("kr_corp_code", name="uq_instrument_kr_corp_code"),
        Index("ix_instrument_market_name", "market", "name"),
    )

    def __repr__(self) -> str:
        return f"<Instrument id={self.instrument_id} {self.market} {self.name!r}>"


class SymbolHistory(Base):
    """A symbol, valid over a window, belonging to one instrument.

    `valid_to` is NULL for the currently active symbol. Lookups resolve a symbol
    as of a point in time rather than assuming today's mapping held then.
    """

    __tablename__ = "symbol_history"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("instrument.instrument_id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)

    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="Where this mapping came from: OBSERVED (we watched it change), "
        "MASTER (historical master file), CORPORATE_ACTION, or SEED. "
        "Anything not OBSERVED is only as trustworthy as its origin.",
    )

    ingested_at: Mapped[IngestedAt]

    instrument: Mapped[Instrument] = relationship(back_populates="symbols")

    __table_args__ = (
        Index("ix_symbol_history_symbol_window", "symbol", "valid_from", "valid_to"),
        Index("ix_symbol_history_instrument", "instrument_id"),
    )

    def __repr__(self) -> str:
        end = self.valid_to or "current"
        return f"<SymbolHistory {self.symbol} {self.valid_from}..{end}>"
