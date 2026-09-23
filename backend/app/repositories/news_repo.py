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

**`record_hits` is the only writer of `news_mention`.** A mention is the
CONFIRMED verdict on a query hit, and the two tables would drift if anything
wrote them separately. So the hit is written first, and the mention set for
those pairs is then made to agree with whatever verdict each hit now holds:
added where CONFIRMED, removed where not. That is also how a re-judgment that
changes its mind reaches the mention table without a second code path.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import case, delete, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.models.news import (
    Decider,
    HitDecision,
    MatchMethod,
    NewsItem,
    NewsMention,
    NewsQueryHit,
    NewsSource,
)
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


class QueryHitRow(NamedTuple):
    """One company's search surfacing one article, with the verdict on it."""

    news_item_id: int
    instrument_id: int
    matched_query: str
    decision: HitDecision
    decision_reason: str
    match_method: MatchMethod | None
    snippet: str | None
    rule_version: int
    decided_by: Decider
    decided_at: datetime


class HitWrite(NamedTuple):
    """How the mention table moved as a result of one `record_hits` call."""

    mentions_added: int
    mentions_removed: int


class HitToJudge(NamedTuple):
    """A stored hit with the text its verdict read.

    `snippet` is the hit's own, not the article's summary. The summary is
    whatever the first search to store the article returned, and another
    company's search may have read a different cut of the same article.
    """

    news_item_id: int
    instrument_id: int
    matched_query: str
    title: str
    snippet: str | None


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


def record_hits(session: Session, rows: Sequence[QueryHitRow]) -> HitWrite:
    """Store verdicts on query hits, then make `news_mention` agree with them.

    A pair seen again is judged again in place. Two rules decide what an
    incoming verdict may overwrite:

    - A rule never overrules a model. Once a hit has been judged by the LLM, a
      later sweep that re-reads the same article must not put it back to what
      the rule thought.
    - `decided_at` moves only when the verdict does. A re-judgment that agrees
      is not a new decision, and reconstructing an earlier moment depends on
      the time a verdict was first reached.
    """
    if not rows:
        return HitWrite(0, 0)

    # One statement cannot update the same row twice, and a sweep can meet the
    # same pair twice when two pages overlap. The last verdict wins.
    by_pair: dict[tuple[int, int], QueryHitRow] = {}
    for row in rows:
        by_pair[(row.news_item_id, row.instrument_id)] = row
    unique = list(by_pair.values())

    table = NewsQueryHit.__table__
    for batch in bulk.batched(unique, columns=len(QueryHitRow._fields)):
        stmt = pg_insert(NewsQueryHit).values([r._asdict() for r in batch])
        incoming = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            constraint="uq_news_query_hit_item_instrument",
            set_={
                "matched_query": incoming.matched_query,
                "decision": incoming.decision,
                "decision_reason": incoming.decision_reason,
                "match_method": incoming.match_method,
                "snippet": incoming.snippet,
                "rule_version": incoming.rule_version,
                "decided_by": incoming.decided_by,
                "decided_at": case(
                    (table.c.decision != incoming.decision, incoming.decided_at),
                    else_=table.c.decided_at,
                ),
            },
            where=or_(
                table.c.decided_by == Decider.RULE.value,
                incoming.decided_by == Decider.LLM.value,
            ),
        )
        session.execute(stmt)

    return _sync_mentions(session, list(by_pair))


# Pairs per `(a, b) IN (...)`. Not the parameter ceiling, which would allow
# 32,767: Postgres refused 8,000 pairs with `statement_too_complex` (the parser
# ran out of stack) and accepted 4,000. A full sweep writes about 30,000.
_PAIRS_PER_STATEMENT = 1000


def _pair_chunks(pairs: Sequence[tuple[int, int]]) -> list[Sequence[tuple[int, int]]]:
    return [
        pairs[start : start + _PAIRS_PER_STATEMENT]
        for start in range(0, len(pairs), _PAIRS_PER_STATEMENT)
    ]


def _sync_mentions(session: Session, pairs: list[tuple[int, int]]) -> HitWrite:
    """Make the mention set for these pairs equal to their CONFIRMED hits."""
    current: dict[tuple[int, int], tuple[HitDecision, str, MatchMethod | None]] = {}
    for chunk in _pair_chunks(pairs):
        found = session.execute(
            select(
                NewsQueryHit.news_item_id,
                NewsQueryHit.instrument_id,
                NewsQueryHit.decision,
                NewsQueryHit.matched_query,
                NewsQueryHit.match_method,
            ).where(tuple_(NewsQueryHit.news_item_id, NewsQueryHit.instrument_id).in_(list(chunk)))
        )
        for item_id, instrument_id, decision, query, method in found.all():
            current[(item_id, instrument_id)] = (decision, query, method)

    not_confirmed = [pair for pair, (d, _, _) in current.items() if d is not HitDecision.CONFIRMED]
    removed = 0
    for chunk in _pair_chunks(not_confirmed):
        result = session.execute(
            delete(NewsMention).where(
                tuple_(NewsMention.news_item_id, NewsMention.instrument_id).in_(list(chunk))
            )
        )
        removed += int(getattr(result, "rowcount", 0) or 0)

    confirmed = [
        {
            "news_item_id": item_id,
            "instrument_id": instrument_id,
            "matched_query": query,
            "match_method": method,
        }
        for (item_id, instrument_id), (d, query, method) in current.items()
        if d is HitDecision.CONFIRMED and method is not None
    ]
    added = 0
    for batch in bulk.batched(confirmed, columns=4):
        stmt = pg_insert(NewsMention).values(list(batch))
        stmt = stmt.on_conflict_do_nothing(constraint="uq_news_mention_item_instrument")
        added += len(session.execute(stmt.returning(NewsMention.id)).scalars().all())

    return HitWrite(mentions_added=added, mentions_removed=removed)


def rule_hits_before(
    session: Session,
    *,
    rule_version: int,
    instrument_ids: Collection[int] | None = None,
) -> list[HitToJudge]:
    """RULE verdicts reached by an older rule, with the text they read.

    Model verdicts are left alone: a rule does not overrule them. Hits with no
    snippet are returned too, so the caller can count what it had to skip.
    `instrument_ids` narrows the set, which is how tests re-judge their own
    rows without touching anyone else's.
    """
    stmt = (
        select(
            NewsQueryHit.news_item_id,
            NewsQueryHit.instrument_id,
            NewsQueryHit.matched_query,
            NewsItem.title,
            NewsQueryHit.snippet,
        )
        .join(NewsItem, NewsItem.id == NewsQueryHit.news_item_id)
        .where(
            NewsQueryHit.decided_by == Decider.RULE,
            NewsQueryHit.rule_version < rule_version,
        )
        .order_by(NewsQueryHit.instrument_id, NewsQueryHit.news_item_id)
    )
    if instrument_ids is not None:
        stmt = stmt.where(NewsQueryHit.instrument_id.in_(list(instrument_ids)))
    return [HitToJudge(*row) for row in session.execute(stmt).all()]


def projection_drift(session: Session) -> tuple[int, int]:
    """(CONFIRMED hits with no mention, mentions with no CONFIRMED hit).

    Both must be zero. Anything else means `news_mention` was written around
    `record_hits`, which is the drift the single write path exists to prevent.
    """
    confirmed = select(NewsQueryHit.news_item_id, NewsQueryHit.instrument_id).where(
        NewsQueryHit.decision == HitDecision.CONFIRMED
    )
    mentioned = select(NewsMention.news_item_id, NewsMention.instrument_id)
    missing = session.execute(
        select(func.count()).select_from(confirmed.except_(mentioned).subquery())
    ).scalar_one()
    orphaned = session.execute(
        select(func.count()).select_from(mentioned.except_(confirmed).subquery())
    ).scalar_one()
    return int(missing), int(orphaned)


def hit_counts(session: Session) -> dict[HitDecision, int]:
    """How many hits hold each verdict. For the CLI and the rollout."""
    found = session.execute(
        select(NewsQueryHit.decision, func.count()).group_by(NewsQueryHit.decision)
    )
    return {decision: int(n) for decision, n in found.all()}


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
