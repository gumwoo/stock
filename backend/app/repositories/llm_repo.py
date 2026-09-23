"""The LLM call ledger and the sentiment readings. See `app.models.llm`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.llm import LlmCall
from app.models.news import NewsSentiment, SentimentEvent
from app.repositories import bulk


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


def save_readings(session: Session, rows: Sequence[SentimentRow]) -> int:
    """Store readings, never replacing one. Returns how many were new."""
    written = 0
    for batch in bulk.batched(rows, columns=len(SentimentRow._fields)):
        stmt = pg_insert(NewsSentiment).values([r._asdict() for r in batch])
        stmt = stmt.on_conflict_do_nothing(constraint="uq_news_sentiment_reading")
        written += len(session.execute(stmt.returning(NewsSentiment.id)).scalars().all())
    return written
