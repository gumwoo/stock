"""The market regime beside each signal: read the index closes of the moment, classify, file.

The index is the one the instrument trades against: KOSDAQ names against
KOSDAQ, other Korean names against KOSPI, US names against the S&P 500.

Everything read is a daily close complete by the signal's `decision_at`. The
arrival time of the bars is not asked: an index close is a public fact that
does not get restated, so a regime can be filed for a signal made before the
index was collected — which is what `backfill` does, and why the table could
be added after the forward record began.

Breadth is read over the names tracked at that moment (a name promoted later
was not), from their own daily bars.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.calendar import Market, MarketCalendar
from app.core.clock import ensure_utc
from app.models import Instrument, Interval, Signal, SignalRegime
from app.models.instrument import Listing
from app.repositories import candle_repo, instrument_repo, market_index_repo, promotion_repo
from app.scoring.regime import Regime, RegimeParams, breadth, classify

logger = logging.getLogger(__name__)

PARAMS = RegimeParams()


def index_for(market: Market, listing: Listing | None) -> str:
    if market is Market.US:
        return "^GSPC"
    return "^KQ11" if listing is Listing.KOSDAQ else "^KS11"


def regime_at(session: Session, index_code: str, asof: datetime) -> Regime:
    closes = market_index_repo.closes_asof(
        session, index_code, asof=asof, limit=PARAMS.history_needed
    )
    return classify(closes, PARAMS)


def breadth_at(session: Session, market: Market, asof: datetime) -> tuple[float | None, int]:
    asof = ensure_utc(asof, field="asof")
    later = promotion_repo.promoted_after(session, asof)
    names = [
        i.instrument_id
        for i in instrument_repo.list_active(session, asof=asof.date(), market=market, tracked=True)
        if i.instrument_id not in later
    ]
    closes = [
        [
            float(c.close)
            for c in candle_repo.history(
                session,
                n,
                Interval.DAY_1,
                limit=PARAMS.breadth_window,
                available_before=asof,
            )
        ]
        for n in names
    ]
    return breadth(closes, PARAMS)


class Cache:
    """Reads shared by signals made at the same moment in the same market."""

    def __init__(self) -> None:
        self.regimes: dict[tuple[str, datetime], Regime] = {}
        self.breadths: dict[tuple[Market, datetime], tuple[float | None, int]] = {}


class IndexNotYetInError(Exception):
    """The index's bar for the last session closed by the moment is not stored yet."""


def _last_close_by(market: Market, asof: datetime) -> datetime | None:
    calendar = MarketCalendar(market)
    today = calendar.local_today(asof)
    for day in reversed(calendar.sessions_between(today - timedelta(days=14), today)):
        if calendar.session_close(day) <= asof:
            return calendar.session_close(day)
    return None


def _row(session: Session, signal: Signal, instrument: Instrument, cache: Cache) -> SignalRegime:
    code = index_for(instrument.market, instrument.listing)
    moment = signal.decision_at
    # Yesterday's close filed as today's regime would look like a reading of
    # today; better no row, which a later backfill fills once the bar is in.
    expected = _last_close_by(instrument.market, moment)
    newest = market_index_repo.latest_available(session, code, asof=moment)
    if expected is not None and (newest is None or newest < expected):
        raise IndexNotYetInError(f"{code}: newest bar by {moment} completed {newest}")
    if (code, moment) not in cache.regimes:
        cache.regimes[(code, moment)] = regime_at(session, code, moment)
    if (instrument.market, moment) not in cache.breadths:
        cache.breadths[(instrument.market, moment)] = breadth_at(session, instrument.market, moment)
    regime = cache.regimes[(code, moment)]
    share, measured = cache.breadths[(instrument.market, moment)]
    return SignalRegime(
        signal_id=signal.id,
        asof=signal.decision_at,
        regime_version=PARAMS.version,
        index_code=code,
        label=regime.label,
        index_close=regime.close,
        trend_gap=regime.trend_gap,
        return_20d=regime.return_20d,
        volatility_20d=regime.volatility,
        volatility_rank=regime.volatility_rank,
        breadth=share,
        breadth_names=measured,
    )


def attach(session: Session, signal: Signal, cache: Cache | None = None) -> SignalRegime | None:
    """File the regime at the signal's decision moment beside it. Commits.

    A failure costs the signal nothing: it is logged and the signal stands.
    Pass one `cache` across the signals of one scoring run.
    """
    try:
        instrument = instrument_repo.get_by_id(session, signal.instrument_id)
        if instrument is None:
            return None
        row = _row(session, signal, instrument, cache or Cache())
        session.add(row)
        session.commit()
        return row
    except IndexNotYetInError as exc:
        logger.warning("regime for signal %s waits for the index: %s", signal.id, exc)
        return None
    except Exception:
        session.rollback()
        logger.exception("regime for signal %s was not recorded", signal.id)
        return None


def backfill(session: Session, *, signal_ids: Collection[int] | None = None) -> int:
    """File a regime for every signal that has none. Commits. Returns rows filed."""
    stmt = (
        select(Signal, Instrument)
        .join(Instrument, Instrument.instrument_id == Signal.instrument_id)
        .outerjoin(SignalRegime, SignalRegime.signal_id == Signal.id)
        .where(SignalRegime.id.is_(None))
        .order_by(Signal.id)
    )
    if signal_ids is not None:
        stmt = stmt.where(Signal.id.in_(list(signal_ids)))
    cache = Cache()
    filed = 0
    for signal, instrument in session.execute(stmt).all():
        try:
            session.add(_row(session, signal, instrument, cache))
        except IndexNotYetInError as exc:
            logger.warning("regime for signal %s still waits: %s", signal.id, exc)
            continue
        filed += 1
    session.commit()
    return filed
