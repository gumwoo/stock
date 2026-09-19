"""Instrument and symbol-history access.

Resolving a symbol is a point-in-time question, not a lookup. Asking "what is
AAPL?" without saying *when* is how a multi-year backtest ends up attributing
one company's price history to another after a ticker gets reassigned.

So `resolve_symbol` takes an `asof` date and matches against the validity
window. Callers that genuinely want today's mapping say so explicitly.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.calendar import Market
from app.models import Instrument, SymbolHistory


def get_by_id(session: Session, instrument_id: int) -> Instrument | None:
    return session.get(Instrument, instrument_id)


def resolve_symbol(
    session: Session,
    symbol: str,
    market: Market,
    *,
    asof: date,
) -> Instrument | None:
    """Find the instrument that carried `symbol` on `asof`.

    `valid_to IS NULL` means the mapping is still current, so an open-ended row
    matches any date on or after its `valid_from`.
    """
    stmt = (
        select(Instrument)
        .join(SymbolHistory, SymbolHistory.instrument_id == Instrument.instrument_id)
        .where(
            SymbolHistory.symbol == symbol,
            Instrument.market == market,
            SymbolHistory.valid_from <= asof,
            (SymbolHistory.valid_to.is_(None)) | (SymbolHistory.valid_to >= asof),
        )
    )
    return session.execute(stmt).scalars().first()


def current_symbol(session: Session, instrument_id: int) -> str | None:
    """The symbol in force now, i.e. the row with no end date."""
    stmt = select(SymbolHistory.symbol).where(
        SymbolHistory.instrument_id == instrument_id,
        SymbolHistory.valid_to.is_(None),
    )
    return session.execute(stmt).scalars().first()


def list_active(session: Session, *, asof: date) -> list[Instrument]:
    """Instruments listed and not yet delisted as of `asof`.

    This is the point-in-time universe. Using today's listed names for a past
    period silently drops everything that delisted in between and inflates the
    result — the classic survivorship bias.
    """
    stmt = select(Instrument).where(
        (Instrument.listed_at.is_(None)) | (Instrument.listed_at <= asof),
        (Instrument.delisted_at.is_(None)) | (Instrument.delisted_at > asof),
    )
    return list(session.execute(stmt).scalars().all())


def upsert_instrument(
    session: Session,
    *,
    market: Market,
    name: str,
    symbol: str,
    sector: str | None = None,
    us_cik: str | None = None,
    kr_corp_code: str | None = None,
    listed_at: date | None = None,
    symbol_valid_from: date | None = None,
    symbol_source: str = "SEED",
) -> Instrument:
    """Create or update an instrument, keyed on its stable external anchor.

    Matching is done on CIK or DART corp code rather than on the symbol,
    because those are the identifiers that survive a rename. Falling back to
    (market, name) is a convenience for seeding only.
    """
    existing: Instrument | None = None
    if us_cik:
        existing = (
            session.execute(select(Instrument).where(Instrument.us_cik == us_cik)).scalars().first()
        )
    elif kr_corp_code:
        existing = (
            session.execute(select(Instrument).where(Instrument.kr_corp_code == kr_corp_code))
            .scalars()
            .first()
        )

    if existing is None:
        existing = (
            session.execute(
                select(Instrument).where(Instrument.market == market, Instrument.name == name)
            )
            .scalars()
            .first()
        )

    if existing is None:
        existing = Instrument(
            market=market,
            name=name,
            sector=sector,
            us_cik=us_cik,
            kr_corp_code=kr_corp_code,
            listed_at=listed_at,
        )
        session.add(existing)
        session.flush()
    else:
        existing.name = name
        if sector:
            existing.sector = sector
        if us_cik:
            existing.us_cik = us_cik
        if kr_corp_code:
            existing.kr_corp_code = kr_corp_code
        if listed_at:
            existing.listed_at = listed_at

    _ensure_symbol(
        session,
        instrument_id=existing.instrument_id,
        symbol=symbol,
        valid_from=symbol_valid_from or listed_at or date(1970, 1, 1),
        source=symbol_source,
    )
    return existing


def _ensure_symbol(
    session: Session,
    *,
    instrument_id: int,
    symbol: str,
    valid_from: date,
    source: str,
) -> None:
    """Record the current symbol, closing any previous one.

    A change observed here is genuinely observed, so it is recorded as such.
    That distinction matters because SEC publishes no ticker history at all —
    `formerNames` holds former *company names*, not former symbols — so any
    mapping we did not watch happen is only as good as wherever it came from.
    """
    current = (
        session.execute(
            select(SymbolHistory).where(
                SymbolHistory.instrument_id == instrument_id,
                SymbolHistory.valid_to.is_(None),
            )
        )
        .scalars()
        .first()
    )

    if current is not None:
        if current.symbol == symbol:
            return
        # The ticker changed under us. Close the old window the day before the
        # new one opens rather than deleting it: the old mapping was true then.
        current.valid_to = valid_from
        source = "OBSERVED"

    session.add(
        SymbolHistory(
            instrument_id=instrument_id,
            symbol=symbol,
            valid_from=valid_from,
            valid_to=None,
            source=source,
        )
    )
