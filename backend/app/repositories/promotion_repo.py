"""Recording promotions, and answering which names were tracked at a moment.

`instrument.tracked` is today's answer. A name promoted after a moment was not
tracked at it, and the promotion rows are the only place that says so.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.models import Instrument
from app.models.promotion import InstrumentPromotion


class PromotionRow(NamedTuple):
    instrument_id: int
    discovered_asof: datetime
    window_hours: int
    baseline_days: float
    recent_mentions: int
    baseline_mentions: int
    score: float
    news_freshness: str
    candle_bars: int
    fundamental_facts: int


def promote(session: Session, row: PromotionRow) -> InstrumentPromotion:
    """Mark the instrument tracked and write the record of why. Does not commit."""
    instrument = session.get(Instrument, row.instrument_id)
    if instrument is None:
        raise ValueError(f"no instrument {row.instrument_id}")
    if instrument.tracked:
        raise ValueError(f"instrument {row.instrument_id} is already tracked")
    instrument.tracked = True
    record = InstrumentPromotion(**row._asdict())
    session.add(record)
    session.flush()
    return record


def promoted_after(session: Session, asof: datetime) -> set[int]:
    """Instruments whose promotion came after `asof`, so were untracked at it."""
    stmt = select(InstrumentPromotion.instrument_id).where(
        InstrumentPromotion.promoted_at > ensure_utc(asof, field="asof")
    )
    return set(session.execute(stmt).scalars())


def history(session: Session, instrument_id: int) -> list[InstrumentPromotion]:
    stmt = (
        select(InstrumentPromotion)
        .where(InstrumentPromotion.instrument_id == instrument_id)
        .order_by(InstrumentPromotion.promoted_at)
    )
    return list(session.execute(stmt).scalars())
