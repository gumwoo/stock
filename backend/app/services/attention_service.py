"""Search attention beside each signal, read from the newest trend fetch stored by then.

The fetch runs with the morning sweep, so a signal at the close reads a
series ending the day before. Only sessions that ended before the moment are
read, and only within what the fetch covered. Korean names only: DataLab is
Naver's.

No backfill: a fetch made today says nothing about what was known when an
older signal was made, so an older signal has no attention rather than a
borrowed one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.calendar import Market, MarketCalendar
from app.core.clock import ensure_utc
from app.models import Signal
from app.models.attention import SignalAttention
from app.repositories import attention_repo, instrument_repo
from app.scoring.attention import DEFAULT, Attention, Status, surge

logger = logging.getLogger(__name__)

PARAMS = DEFAULT


def attention_at(
    session: Session, instrument_id: int, asof: datetime
) -> tuple[Attention, int | None]:
    """The attention on a Korean name at `asof`, and the fetch it was read from."""
    asof = ensure_utc(asof, field="asof")
    trend = attention_repo.latest_asof(session, instrument_id, asof=asof)
    if trend is None:
        return Attention(Status.NO_FETCH), None
    calendar = MarketCalendar(Market.KR)
    yesterday = calendar.local_today(asof) - timedelta(days=1)
    last = min(trend.end_date, yesterday)
    if last < trend.start_date:
        return Attention(Status.UNMEASURED), trend.id
    sessions = calendar.sessions_between(trend.start_date, last)
    # A fetch that stops short of the last session before the moment is an
    # old picture: mornings the fetch failed would otherwise read as quiet.
    recent = calendar.sessions_between(yesterday - timedelta(days=14), yesterday)
    if recent and (not sessions or sessions[-1] < recent[-1]):
        return Attention(Status.STALE), trend.id
    return surge(trend.series, sessions, PARAMS), trend.id


def attach(session: Session, signal: Signal) -> SignalAttention | None:
    """File the attention at the signal's decision moment beside it. Commits.

    A failure costs the signal nothing: it is logged and the signal stands.
    """
    try:
        instrument = instrument_repo.get_by_id(session, signal.instrument_id)
        if instrument is None or instrument.market is not Market.KR:
            return None
        found, trend_id = attention_at(session, signal.instrument_id, signal.decision_at)
        row = SignalAttention(
            signal_id=signal.id,
            asof=signal.decision_at,
            attention_version=PARAMS.version,
            trend_id=trend_id,
            surge=found.surge,
            recent=found.recent,
            baseline=found.baseline,
            status=found.status,
        )
        session.add(row)
        session.commit()
        return row
    except Exception:
        session.rollback()
        logger.exception("attention for signal %s was not recorded", signal.id)
        return None
