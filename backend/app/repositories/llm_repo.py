"""The LLM call ledger and the sentiment readings. See `app.models.llm`."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.models.llm import LlmCall
from app.models.news import NewsItem, NewsSentiment, SentimentEvent
from app.repositories import bulk, news_repo


class LlmCallRow(NamedTuple):
    purpose: str
    provider: str
    model: str
    prompt_version: int
    items: int
    status: str
    input_tokens: int = 0
    output_tokens: int = 0
    notional_cost_usd: float | None = None
    five_hour_utilization: float | None = None
    seven_day_utilization: float | None = None
    error: str | None = None


def recent_utilization(session: Session, *, provider: str) -> tuple[float | None, float | None]:
    """The subscription's fullness as the last calls through `provider` reported it.

    Five-hour usage from a call inside the last five hours; seven-day usage
    from a call inside the last day. Older readings say nothing about now.
    """
    five = session.execute(
        select(LlmCall.five_hour_utilization)
        .where(
            LlmCall.provider == provider,
            LlmCall.five_hour_utilization.is_not(None),
            LlmCall.called_at > func.clock_timestamp() - text("interval '5 hours'"),
        )
        .order_by(LlmCall.called_at.desc())
        .limit(1)
    ).scalar()
    seven = session.execute(
        select(LlmCall.seven_day_utilization)
        .where(
            LlmCall.provider == provider,
            LlmCall.seven_day_utilization.is_not(None),
            LlmCall.called_at > func.clock_timestamp() - text("interval '1 day'"),
        )
        .order_by(LlmCall.called_at.desc())
        .limit(1)
    ).scalar()
    return five, seven


def record_call(session: Session, row: LlmCallRow) -> None:
    """Append one call. Does not commit; the caller commits it with the batch."""
    session.add(LlmCall(**row._asdict()))


class SentimentRow(NamedTuple):
    news_item_id: int
    instrument_id: int
    model: str
    prompt_version: int
    sentiment: float
    event_type: SentimentEvent
    intensity: float
    confidence: float
    evidence: str
    material: bool | None = None


def save_readings(session: Session, rows: Sequence[SentimentRow]) -> int:
    """Store readings, never replacing one. Returns how many were new."""
    written = 0
    for batch in bulk.batched(rows, columns=len(SentimentRow._fields)):
        stmt = pg_insert(NewsSentiment).values([r._asdict() for r in batch])
        stmt = stmt.on_conflict_do_nothing(constraint="uq_news_sentiment_reading")
        written += len(session.execute(stmt.returning(NewsSentiment.id)).scalars().all())
    return written


class ReadingAsOf(NamedTuple):
    instrument_id: int
    news_item_id: int
    available_at: datetime
    title: str
    event_type: str
    sentiment: float
    intensity: float
    confidence: float
    material: bool | None = None


def readings_asof(
    session: Session,
    *,
    asof: datetime,
    since: datetime,
    model: str,
    prompt_version: int,
    instrument_ids: Collection[int],
) -> tuple[list[ReadingAsOf], dict[int, int]]:
    """Readings usable at `asof`, and per instrument how many confirmed articles had none.

    A reading counts only if everything it rests on existed at `asof`: the
    article was available and stored, the reading had been made, and the
    relevance verdict standing at `asof` was CONFIRMED — a reading of an
    article later judged not to be about the company drops out from then on,
    and stays in for any moment before. One model and prompt version at a
    time, so two prompts' readings of one article are never added together.
    """
    asof = ensure_utc(asof, field="asof")
    since = ensure_utc(since, field="since")
    confirmed = news_repo.confirmed_pairs_asof(
        session, asof=asof, since=since, instrument_ids=instrument_ids
    )
    if not confirmed:
        return [], {}
    r = NewsSentiment
    rows = session.execute(
        select(
            r.instrument_id,
            r.news_item_id,
            NewsItem.available_at,
            NewsItem.title,
            r.event_type,
            r.sentiment,
            r.intensity,
            r.confidence,
            r.material,
        )
        .join(NewsItem, NewsItem.id == r.news_item_id)
        .where(
            r.model == model,
            r.prompt_version == prompt_version,
            r.created_at <= asof,
            r.instrument_id.in_(list(instrument_ids)),
            NewsItem.available_at > since,
            NewsItem.available_at <= asof,
        )
    ).all()
    readings = [
        ReadingAsOf(
            instrument_id=row[0],
            news_item_id=row[1],
            available_at=row[2],
            title=row[3],
            event_type=getattr(row[4], "value", str(row[4])),
            sentiment=row[5],
            intensity=row[6],
            confidence=row[7],
            material=row[8],
        )
        for row in rows
        if (row[0], row[1]) in confirmed
    ]
    read = {(x.instrument_id, x.news_item_id) for x in readings}
    unread: dict[int, int] = {}
    for instrument_id, item_id in confirmed:
        if (instrument_id, item_id) not in read:
            unread[instrument_id] = unread.get(instrument_id, 0) + 1
    return readings, unread
