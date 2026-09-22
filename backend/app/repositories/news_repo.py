"""Storing articles and the instruments they mention.

Two writes, in order: articles first, then mentions that point at them. The
article table is keyed on the canonical URL's hash, so an article surfaced by
three different queries is stored once and carries three mentions.

**`save_news_items` returns ids for every row, not just the inserted ones.**
This is where the `filing_repo` template does not carry over. `ON CONFLICT DO
NOTHING ... RETURNING id` returns only rows it actually inserted, and filings
never needed anything back. Mentions do: the common case, once two instruments
share an article, is attaching a mention to a row this run did not insert. So
the insert is followed by a lookup that fills in the ids for the whole batch.

`ON CONFLICT DO UPDATE` with a no-op update would also return every id, and is
the wrong trade — it writes a dead tuple per conflict, and conflicts are the
normal case here rather than the exception.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.models.news import MatchMethod, NewsItem, NewsMention, NewsSource
from app.repositories import bulk


class NewsItemRow(NamedTuple):
    source: NewsSource
    url_hash: str
    url: str
    naver_url: str | None
    publisher_host: str | None
    title: str
    summary: str | None
    published_at: datetime
    available_at: datetime


class NewsMentionRow(NamedTuple):
    news_item_id: int
    instrument_id: int
    matched_query: str
    match_method: MatchMethod


def save_news_items(session: Session, rows: Sequence[NewsItemRow]) -> tuple[int, dict[str, int]]:
    """Insert articles, ignoring ones already stored.

    Returns how many were newly written, and `url_hash -> id` for **every** row
    passed in, whether or not this call inserted it. Mentions need the second
    part; see the module docstring for why the insert alone cannot supply it.
    """
    if not rows:
        return 0, {}

    written = 0
    for batch in bulk.batched(rows, columns=len(NewsItemRow._fields)):
        stmt = pg_insert(NewsItem).values([r._asdict() for r in batch])
        stmt = stmt.on_conflict_do_nothing(constraint="uq_news_item_source_url_hash")
        written += len(session.execute(stmt.returning(NewsItem.id)).scalars().all())

    ids: dict[str, int] = {}
    by_source: dict[NewsSource, list[str]] = {}
    for row in rows:
        by_source.setdefault(row.source, []).append(row.url_hash)

    for source, hashes in by_source.items():
        for chunk in bulk.batched(hashes, columns=1):
            found = session.execute(
                select(NewsItem.url_hash, NewsItem.id).where(
                    NewsItem.source == source, NewsItem.url_hash.in_(list(chunk))
                )
            )
            for url_hash, item_id in found.all():
                ids[url_hash] = item_id

    return written, ids


def save_mentions(session: Session, rows: Sequence[NewsMentionRow]) -> int:
    """Attach instruments to articles, ignoring links already recorded."""
    if not rows:
        return 0

    written = 0
    for batch in bulk.batched(rows, columns=len(NewsMentionRow._fields)):
        stmt = pg_insert(NewsMention).values([r._asdict() for r in batch])
        stmt = stmt.on_conflict_do_nothing(constraint="uq_news_mention_item_instrument")
        written += len(session.execute(stmt.returning(NewsMention.id)).scalars().all())
    return written


def latest_available_at(
    session: Session,
    *,
    source: NewsSource | None = None,
    instrument_id: int | None = None,
    ingested_before: datetime | None = None,
) -> datetime | None:
    """The newest article we can see, for judging whether the pipe is flowing.

    This is the `source_asof` a `WallClockFreshnessRule` wants: news and social
    should keep arriving, so silence is itself the signal. Distinct from the
    fundamental rule, which asks when the source was last checked rather than
    how old the newest item is.
    """
    stmt = select(func.max(NewsItem.available_at))
    if source is not None:
        stmt = stmt.where(NewsItem.source == source)
    if ingested_before is not None:
        stmt = stmt.where(
            NewsItem.ingested_at <= ensure_utc(ingested_before, field="ingested_before")
        )
    if instrument_id is not None:
        stmt = stmt.join(NewsMention, NewsMention.news_item_id == NewsItem.id).where(
            NewsMention.instrument_id == instrument_id
        )
    return session.execute(stmt).scalar()


def count_items(session: Session, *, source: NewsSource | None = None) -> int:
    stmt = select(func.count()).select_from(NewsItem)
    if source is not None:
        stmt = stmt.where(NewsItem.source == source)
    return int(session.execute(stmt).scalar_one())
