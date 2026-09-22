"""Instrument and symbol-history access.

Resolving a symbol is a point-in-time question, not a lookup. Asking "what is
AAPL?" without saying *when* is how a multi-year backtest ends up attributing
one company's price history to another after a ticker gets reassigned.

So `resolve_symbol` takes an `asof` date and matches against the validity
window. Callers that genuinely want today's mapping say so explicitly.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.calendar import Market
from app.core.clock import utc_now
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

    Windows are closed intervals: `valid_from <= asof <= valid_to`. A row with
    `valid_to IS NULL` is the currently active mapping and matches any date on
    or after its `valid_from`. Writers must close a superseded window on the
    day before its successor opens, or a changeover date would match twice.
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


def list_active(
    session: Session,
    *,
    asof: date,
    market: Market | None = None,
    tracked: bool | None = None,
) -> list[Instrument]:
    """Instruments listed and not yet delisted as of `asof`.

    This is the point-in-time universe. Using today's listed names for a past
    period silently drops everything that delisted in between and inflates the
    result — the classic survivorship bias.

    `tracked` splits two different questions and both callers exist. Scoring
    and cross-sectional ranking want `tracked=True`: a name we know only from
    a listing master has no prices and no financials, so putting it in a peer
    group is not a comparison, and scoring it is not possible. News collection
    wants every row, because finding a company worth looking at is the whole
    point of reading the news. Left unset, everything comes back.
    """
    stmt = select(Instrument).where(
        (Instrument.listed_at.is_(None)) | (Instrument.listed_at <= asof),
        (Instrument.delisted_at.is_(None)) | (Instrument.delisted_at > asof),
    )
    if market is not None:
        stmt = stmt.where(Instrument.market == market)
    if tracked is not None:
        stmt = stmt.where(Instrument.tracked == tracked)
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
    tracked: bool | None = None,
) -> Instrument:
    """Create or update an instrument, keyed on its stable external anchor.

    Matching is done on CIK or DART corp code rather than on the symbol,
    because those are the identifiers that survive a rename.

    **The (market, name) fallback runs only when no anchor was given.** Korean
    company names are not unique — the real `corpCode.xml` holds thirty pairs
    of listed companies sharing a name, among them SK and 삼성물산 — so a
    caller that supplied a corp code and still fell through to the name would
    merge two different companies onto one row, overwrite the anchor with the
    second one's, and leave the symbol history with a window that ends before
    it starts. That row's symbol then resolves to nothing, permanently.

    `tracked` says whether this name is scoreable. Left unset it is not
    changed, and a new row starts untracked: a listing master establishes that
    a company exists, not that anyone follows it. Seeding passes True.
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
    elif not us_cik and not kr_corp_code:
        existing = (
            session.execute(
                select(Instrument).where(Instrument.market == market, Instrument.name == name)
            )
            .scalars()
            .first()
        )

    if existing is None and (us_cik or kr_corp_code):
        # A row of this name carrying no anchor at all is this same company
        # from before anyone knew its code, so adopt it rather than opening a
        # second row beside it with the same symbol. A row that already has an
        # anchor is a different company that happens to share the name — the
        # real master holds thirty such pairs — and merging those is exactly
        # what the lookup above exists to prevent.
        existing = (
            session.execute(
                select(Instrument).where(
                    Instrument.market == market,
                    Instrument.name == name,
                    Instrument.us_cik.is_(None),
                    Instrument.kr_corp_code.is_(None),
                )
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
            tracked=bool(tracked),
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
        if tracked is not None:
            existing.tracked = tracked

    _ensure_symbol(
        session,
        instrument_id=existing.instrument_id,
        market=market,
        symbol=symbol,
        valid_from=symbol_valid_from,
        first_window_from=listed_at or date(1970, 1, 1),
        observed_on=utc_now().date(),
        source=symbol_source,
    )
    return existing


def _earliest_free(
    session: Session,
    *,
    instrument_id: int,
    market: Market,
    symbol: str,
    earliest: date,
    observed_on: date,
) -> date:
    """The first day this instrument may claim `symbol`, closing who held it.

    Tickers get reassigned. Korea recycles six-digit codes and the US recycles
    letters, and nothing in either feed announces it — a listing master simply
    shows the code against a different company than last month.

    The danger is not the reassignment, it is the window we would open for the
    new holder. A collector that does not know when the change happened opens
    at the placeholder date, which is 1970, which covers every day the previous
    holder legitimately owned the code. `resolve_symbol` then matches two rows
    for a historical date and returns whichever the planner ordered first, so a
    backtest attributes one company's prices to another. That is the failure
    the whole point-in-time symbol table exists to prevent, arriving through
    the table itself.

    So: the newcomer starts no earlier than today and no earlier than the day
    after any window already recorded for that code, and whoever still holds it
    is closed out the day before. No exception is raised even when two
    companies claim the code on one day, because one contradictory row must not
    cost a sweep of several thousand.
    """
    held = list(
        session.execute(
            select(SymbolHistory)
            .join(Instrument, Instrument.instrument_id == SymbolHistory.instrument_id)
            .where(
                SymbolHistory.symbol == symbol,
                Instrument.market == market,
                SymbolHistory.instrument_id != instrument_id,
            )
        )
        .scalars()
        .all()
    )
    if not held:
        return earliest

    bounds = [earliest, observed_on]
    for row in held:
        bounds.append(row.valid_from + timedelta(days=1))
        if row.valid_to is not None:
            bounds.append(row.valid_to + timedelta(days=1))
    start = max(bounds)

    for row in held:
        if row.valid_to is None:
            row.valid_to = start - timedelta(days=1)
    return start


def _ensure_symbol(
    session: Session,
    *,
    instrument_id: int,
    market: Market,
    symbol: str,
    valid_from: date | None,
    first_window_from: date,
    observed_on: date,
    source: str,
) -> None:
    """Record the current symbol, closing any previous one.

    A change observed here is genuinely observed, so it is recorded as such.
    That distinction matters because SEC publishes no ticker history at all —
    `formerNames` holds former *company names*, not former symbols — so any
    mapping we did not watch happen is only as good as wherever it came from.

    **`valid_from=None` means the caller does not know when.** It is the usual
    case: a collector reading today's listing master sees a symbol, not a
    changeover date. The two situations need different answers and previously
    got the same one. Opening the *first* window with no date means "as far
    back as we care", which is `first_window_from`. Noticing a *change* with no
    date means "different as of today", which is `observed_on` — reusing the
    first-window placeholder there would close the live window on a day before
    it opened, and every master row shares that placeholder, so it would fire
    for every company that ever changed code.
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

    if current is None:
        session.add(
            SymbolHistory(
                instrument_id=instrument_id,
                symbol=symbol,
                valid_from=_earliest_free(
                    session,
                    instrument_id=instrument_id,
                    market=market,
                    symbol=symbol,
                    earliest=valid_from if valid_from is not None else first_window_from,
                    observed_on=observed_on,
                ),
                valid_to=None,
                source=source,
            )
        )
        return

    if current.symbol == symbol:
        return

    if valid_from is None:
        # Never before the day after the window it replaces. `observed_on`
        # alone would be enough in practice, but a window opened with a future
        # `listed_at` would still invert, and the inversion is silent.
        changeover = max(observed_on, current.valid_from + timedelta(days=1))
    elif valid_from <= current.valid_from:
        # A date the caller chose, and it is on or before the window it
        # replaces. That closes the live window a day earlier than it opened,
        # after which `resolve_symbol` matches neither row and the instrument
        # has no symbol on any date at all. Refuse rather than write it:
        # silence here is a lookup that fails forever.
        raise ValueError(
            f"instrument {instrument_id}: symbol {symbol!r} would start "
            f"{valid_from}, on or before the current window for "
            f"{current.symbol!r} which opened {current.valid_from}"
        )
    else:
        changeover = valid_from

    changeover = _earliest_free(
        session,
        instrument_id=instrument_id,
        market=market,
        symbol=symbol,
        earliest=changeover,
        observed_on=observed_on,
    )

    # The ticker changed under us. Close the old window on the day *before* the
    # new one opens rather than deleting it: the old mapping was true then.
    # Both bounds are inclusive, matching the `valid_from <= asof <= valid_to`
    # lookup, so closing on `changeover` itself would leave the changeover date
    # resolving to two instruments at once.
    current.valid_to = changeover - timedelta(days=1)

    session.add(
        SymbolHistory(
            instrument_id=instrument_id,
            symbol=symbol,
            valid_from=changeover,
            valid_to=None,
            source="OBSERVED",
        )
    )
