"""Search trend fetches: stored whole, read as they stood at a moment."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.models.attention import SearchTrend


def save_trend(
    session: Session,
    *,
    instrument_id: int,
    start_date: date,
    end_date: date,
    keywords: Sequence[str],
    series: dict[str, float],
) -> SearchTrend:
    """Store one fetch. The database stamps when. Does not commit."""
    row = SearchTrend(
        instrument_id=instrument_id,
        start_date=start_date,
        end_date=end_date,
        keywords=list(keywords),
        series=series,
    )
    session.add(row)
    session.flush()
    return row


def latest_asof(session: Session, instrument_id: int, *, asof: datetime) -> SearchTrend | None:
    """The newest fetch stored by `asof`."""
    t = SearchTrend
    stmt = (
        select(t)
        .where(t.instrument_id == instrument_id, t.fetched_at <= ensure_utc(asof, field="asof"))
        .order_by(t.fetched_at.desc(), t.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()
