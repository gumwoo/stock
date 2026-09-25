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

**`record_hits` is the only writer of `news_mention`.** A mention is a hit
whose latest verdict is CONFIRMED, and the two tables would drift if anything
wrote them separately. So the hit and its verdict are written first, and the
mention set for those pairs is then made to agree with the latest verdicts:
added where CONFIRMED, removed where not. That is also how a re-judgment that
changes its mind reaches the mention table without a second code path.

**Verdicts are only ever appended.** The mention table forgets; the decision
table does not. `decisions_asof` answers what was believed at a past moment,
and is what any point-in-time reader must use.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import (
    Subquery,
    and_,
    delete,
    func,
    insert,
    literal,
    or_,
    select,
    tuple_,
    union_all,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.models import Instrument
from app.models.news import (
    Decider,
    HitDecision,
    MatchMethod,
    NewsItem,
    NewsMention,
    NewsQueryHit,
    NewsRelevanceDecision,
    NewsSentiment,
    NewsSource,
    NewsSweepCoverage,
    RuleAudit,
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
    """One company's search surfacing one article, with the verdict on it.

    The write unit of `record_hits`: the hit is stored as a fact, the verdict
    appended as a decision. No time is passed; the database stamps it.
    """

    news_item_id: int
    instrument_id: int
    matched_query: str
    decision: HitDecision
    decision_reason: str
    match_method: MatchMethod | None
    snippet: str | None
    rule_version: int
    decided_by: Decider
    # An LLM verdict's provenance. Left empty by the rule.
    model: str | None = None
    prompt_version: int | None = None
    rationale: str | None = None


class HitWrite(NamedTuple):
    """What one `record_hits` call appended, and how the mention table moved."""

    decisions_added: int
    mentions_added: int
    mentions_removed: int


class HitToJudge(NamedTuple):
    """A hit's latest verdict with the text it read.

    `snippet` is the verdict's own, not the article's summary. The summary is
    whatever the first search to store the article returned, and another
    company's search may have read a different cut of the same article.
    """

    news_item_id: int
    instrument_id: int
    matched_query: str
    title: str
    snippet: str | None


class DecisionAsOf(NamedTuple):
    """The verdict a hit held at some moment."""

    news_item_id: int
    instrument_id: int
    decision: HitDecision
    decision_reason: str
    match_method: MatchMethod | None
    rule_version: int
    decided_by: Decider
    decided_at: datetime


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


# What makes two verdicts the same. A difference in any of these is a new
# decision and gets its own row; none is a re-reading that agrees, and writes
# nothing.
_VERDICT_FIELDS = (
    "decision",
    "decision_reason",
    "match_method",
    "matched_query",
    "snippet",
    "rule_version",
    "decided_by",
    "model",
    "prompt_version",
    "rationale",
)


def record_hits(session: Session, rows: Sequence[QueryHitRow]) -> HitWrite:
    """Store the hits, append the verdicts that changed, then project mentions.

    Nothing is updated in place. A hit is inserted once and never touched; a
    verdict is appended when it differs from the hit's latest one. Two rules
    decide whether an incoming verdict is written at all:

    - A rule never overrules a model. Once the latest verdict is the LLM's, a
      later sweep that re-reads the article with the rule writes nothing.
    - Agreement is not a decision. The same verdict from the same text under
      the same rule adds no row, so the latest row at or before a moment is
      the verdict held at that moment, stamped when it was first reached.
    """
    if not rows:
        return HitWrite(0, 0, 0)

    # A sweep can meet the same pair twice when two pages overlap. The last
    # verdict wins, as it would have had they arrived in separate calls.
    by_pair: dict[tuple[int, int], QueryHitRow] = {}
    for row in rows:
        by_pair[(row.news_item_id, row.instrument_id)] = row

    facts = [
        {
            "news_item_id": r.news_item_id,
            "instrument_id": r.instrument_id,
            "matched_query": r.matched_query,
        }
        for r in by_pair.values()
    ]
    for batch in bulk.batched(facts, columns=3):
        stmt = pg_insert(NewsQueryHit).values(list(batch))
        session.execute(stmt.on_conflict_do_nothing(constraint="uq_news_query_hit_item_instrument"))

    hit_ids = _hit_ids(session, list(by_pair))
    latest = _latest_for(session, list(hit_ids.values()))

    appended: list[dict[str, object]] = []
    for pair, row in by_pair.items():
        hit_id = hit_ids[pair]
        prev = latest.get(hit_id)
        if prev is not None:
            if prev.decided_by is Decider.LLM and row.decided_by is Decider.RULE:
                continue
            if all(getattr(prev, f) == getattr(row, f) for f in _VERDICT_FIELDS):
                continue
        appended.append({"query_hit_id": hit_id, **{f: getattr(row, f) for f in _VERDICT_FIELDS}})
    for batch in bulk.batched(appended, columns=len(_VERDICT_FIELDS) + 1):
        session.execute(pg_insert(NewsRelevanceDecision).values(list(batch)))

    added, removed = _sync_mentions(session, hit_ids)
    return HitWrite(decisions_added=len(appended), mentions_added=added, mentions_removed=removed)


# Pairs per `(a, b) IN (...)`. Not the parameter ceiling, which would allow
# 32,767: Postgres refused 8,000 pairs with `statement_too_complex` (the parser
# ran out of stack) and accepted 4,000. A full sweep writes about 30,000.
_PAIRS_PER_STATEMENT = 1000


def _pair_chunks(pairs: Sequence[tuple[int, int]]) -> list[Sequence[tuple[int, int]]]:
    return [
        pairs[start : start + _PAIRS_PER_STATEMENT]
        for start in range(0, len(pairs), _PAIRS_PER_STATEMENT)
    ]


def _hit_ids(session: Session, pairs: list[tuple[int, int]]) -> dict[tuple[int, int], int]:
    ids: dict[tuple[int, int], int] = {}
    for chunk in _pair_chunks(pairs):
        found = session.execute(
            select(NewsQueryHit.news_item_id, NewsQueryHit.instrument_id, NewsQueryHit.id).where(
                tuple_(NewsQueryHit.news_item_id, NewsQueryHit.instrument_id).in_(list(chunk))
            )
        )
        for item_id, instrument_id, hit_id in found.all():
            ids[(item_id, instrument_id)] = hit_id
    return ids


def _latest(*, asof: datetime | None = None) -> Subquery:
    """Each hit's newest verdict, optionally as it stood at `asof`.

    Ordered by the database's own stamp, then by id: two rows with the same
    stamp were written in that order.
    """
    d = NewsRelevanceDecision
    stmt = (
        select(d)
        .distinct(d.query_hit_id)
        .order_by(d.query_hit_id, d.decided_at.desc(), d.id.desc())
    )
    if asof is not None:
        stmt = stmt.where(d.decided_at <= ensure_utc(asof, field="asof"))
    return stmt.subquery("latest")


def _latest_for(session: Session, hit_ids: Sequence[int]) -> dict[int, Any]:
    d = NewsRelevanceDecision
    out: dict[int, Any] = {}
    for chunk in bulk.batched(hit_ids, columns=1):
        stmt = (
            select(d)
            .distinct(d.query_hit_id)
            .where(d.query_hit_id.in_(list(chunk)))
            .order_by(d.query_hit_id, d.decided_at.desc(), d.id.desc())
        )
        for decision in session.execute(stmt).scalars():
            out[decision.query_hit_id] = decision
    return out


def _sync_mentions(session: Session, hit_ids: dict[tuple[int, int], int]) -> tuple[int, int]:
    """Make the mention set for these pairs equal to their latest CONFIRMED verdicts."""
    latest = _latest_for(session, list(hit_ids.values()))

    confirmed: list[dict[str, object]] = []
    not_confirmed: list[tuple[int, int]] = []
    for pair, hit_id in hit_ids.items():
        verdict = latest.get(hit_id)
        if (
            verdict is not None
            and verdict.decision is HitDecision.CONFIRMED
            and verdict.match_method is not None
        ):
            confirmed.append(
                {
                    "news_item_id": pair[0],
                    "instrument_id": pair[1],
                    "matched_query": verdict.matched_query,
                    "match_method": verdict.match_method,
                }
            )
        else:
            not_confirmed.append(pair)

    removed = 0
    for chunk in _pair_chunks(not_confirmed):
        result = session.execute(
            delete(NewsMention).where(
                tuple_(NewsMention.news_item_id, NewsMention.instrument_id).in_(list(chunk))
            )
        )
        removed += int(getattr(result, "rowcount", 0) or 0)

    added = 0
    for batch in bulk.batched(confirmed, columns=4):
        stmt = pg_insert(NewsMention).values(list(batch))
        stmt = stmt.on_conflict_do_nothing(constraint="uq_news_mention_item_instrument")
        added += len(session.execute(stmt.returning(NewsMention.id)).scalars().all())

    return added, removed


def rule_hits_before(
    session: Session,
    *,
    rule_version: int,
    instrument_ids: Collection[int] | None = None,
) -> list[HitToJudge]:
    """Hits whose latest verdict is a RULE one from an older rule, with its text.

    Model verdicts are left alone: a rule does not overrule them. Verdicts with
    no snippet are returned too, so the caller can count what it had to skip.
    """
    latest = _latest()
    stmt = (
        select(
            NewsQueryHit.news_item_id,
            NewsQueryHit.instrument_id,
            latest.c.matched_query,
            NewsItem.title,
            latest.c.snippet,
        )
        .join(NewsQueryHit, NewsQueryHit.id == latest.c.query_hit_id)
        .join(NewsItem, NewsItem.id == NewsQueryHit.news_item_id)
        .where(
            latest.c.decided_by == Decider.RULE,
            latest.c.rule_version < rule_version,
        )
        .order_by(NewsQueryHit.instrument_id, NewsQueryHit.news_item_id)
    )
    if instrument_ids is not None:
        stmt = stmt.where(NewsQueryHit.instrument_id.in_(list(instrument_ids)))
    return [HitToJudge(*row) for row in session.execute(stmt).all()]


class OpenHit(NamedTuple):
    """A hit waiting for a model: its latest verdict, and the text that verdict read."""

    news_item_id: int
    instrument_id: int
    matched_query: str
    title: str
    snippet: str
    match_method: MatchMethod | None
    rule_version: int
    available_at: datetime


def _open_hits(
    session: Session,
    *,
    decision: HitDecision | None,
    limit: int,
    instrument_ids: Collection[int] | None,
    extra: Any = None,
) -> list[OpenHit]:
    latest = _latest()
    stmt = (
        select(
            NewsQueryHit.news_item_id,
            NewsQueryHit.instrument_id,
            latest.c.matched_query,
            NewsItem.title,
            latest.c.snippet,
            latest.c.match_method,
            latest.c.rule_version,
            NewsItem.available_at,
        )
        .select_from(latest)
        .join(NewsQueryHit, NewsQueryHit.id == latest.c.query_hit_id)
        .join(NewsItem, NewsItem.id == NewsQueryHit.news_item_id)
        .where(latest.c.snippet.is_not(None))
        .order_by(NewsItem.available_at.desc(), NewsQueryHit.id)
        .limit(limit)
    )
    if decision is not None:
        stmt = stmt.where(latest.c.decision == decision)
    if extra is not None:
        stmt = stmt.where(extra(latest))
    if instrument_ids is not None:
        stmt = stmt.where(NewsQueryHit.instrument_id.in_(list(instrument_ids)))
    return [OpenHit(*row) for row in session.execute(stmt).all()]


def pending_for_model(
    session: Session,
    *,
    limit: int,
    prompt_version: int,
    instrument_ids: Collection[int] | None = None,
) -> list[OpenHit]:
    """Hits for the model to judge under `prompt_version`, newest article first.

    PENDING hits the rule left undecided, and every verdict a model reached
    under an older prompt, whatever it was — the first prompt confirmed
    baseball teams and news bylines, and a model verdict is otherwise final.
    Not an UNSURE under the current prompt: asking again gets the same
    answer. Only with a snippet: a verdict carried over from before snippets
    were kept has no record of what it read.
    """
    return _open_hits(
        session,
        decision=None,
        limit=limit,
        instrument_ids=instrument_ids,
        extra=lambda latest: or_(
            and_(
                latest.c.decided_by == Decider.RULE,
                latest.c.decision == HitDecision.PENDING,
            ),
            and_(
                latest.c.decided_by == Decider.LLM,
                latest.c.prompt_version < prompt_version,
            ),
        ),
    )


class AuditTarget(NamedTuple):
    """A hit the rule confirmed, put to the model as a check on the rule."""

    hit: OpenHit
    query_hit_id: int
    decision_reason: str


def rule_confirmed_sample(
    session: Session,
    *,
    limit: int,
    model: str,
    prompt_version: int,
    rule_version: int,
    instrument_ids: Collection[int] | None = None,
) -> list[AuditTarget]:
    """A random sample of hits whose latest verdict is the rule's CONFIRMED.

    Random, not newest: the question is how often the rule is right across
    what it confirms, and the newest hits are one day's news. Hits already
    audited by this model under this prompt are left out, so repeated audits
    widen the sample instead of re-asking it.
    """
    latest = _latest()
    audited = (
        select(RuleAudit.id)
        .where(
            RuleAudit.query_hit_id == NewsQueryHit.id,
            RuleAudit.rule_version == latest.c.rule_version,
            RuleAudit.model == model,
            RuleAudit.prompt_version == prompt_version,
        )
        .exists()
    )
    stmt = (
        select(
            NewsQueryHit.news_item_id,
            NewsQueryHit.instrument_id,
            latest.c.matched_query,
            NewsItem.title,
            latest.c.snippet,
            latest.c.match_method,
            latest.c.rule_version,
            NewsItem.available_at,
            NewsQueryHit.id,
            latest.c.decision_reason,
        )
        .select_from(latest)
        .join(NewsQueryHit, NewsQueryHit.id == latest.c.query_hit_id)
        .join(NewsItem, NewsItem.id == NewsQueryHit.news_item_id)
        .where(
            latest.c.decision == HitDecision.CONFIRMED,
            latest.c.decided_by == Decider.RULE,
            latest.c.rule_version == rule_version,
            latest.c.snippet.is_not(None),
            ~audited,
        )
        .order_by(func.random())
        .limit(limit)
    )
    if instrument_ids is not None:
        stmt = stmt.where(NewsQueryHit.instrument_id.in_(list(instrument_ids)))
    return [AuditTarget(OpenHit(*row[:8]), row[8], row[9]) for row in session.execute(stmt).all()]


def confirmed_unread(
    session: Session,
    *,
    model: str,
    prompt_version: int,
    limit: int,
    instrument_ids: Collection[int] | None = None,
) -> list[OpenHit]:
    """CONFIRMED hits no reading exists for under this model and prompt, newest first."""

    def unread(latest: Any) -> Any:
        return ~(
            select(NewsSentiment.id)
            .where(
                NewsSentiment.news_item_id == NewsQueryHit.news_item_id,
                NewsSentiment.instrument_id == NewsQueryHit.instrument_id,
                NewsSentiment.model == model,
                NewsSentiment.prompt_version == prompt_version,
            )
            .exists()
        )

    return _open_hits(
        session,
        decision=HitDecision.CONFIRMED,
        limit=limit,
        instrument_ids=instrument_ids,
        extra=unread,
    )


def decisions_asof(
    session: Session,
    asof: datetime,
    *,
    instrument_ids: Collection[int] | None = None,
) -> list[DecisionAsOf]:
    """Every hit's verdict as it stood at `asof`.

    The read a forward test or a reproduction must use. `news_mention` holds
    only today's opinion and forgets a withdrawn mention; this remembers it.
    A hit with no verdict written by `asof` is absent.
    """
    latest = _latest(asof=asof)
    stmt = select(
        NewsQueryHit.news_item_id,
        NewsQueryHit.instrument_id,
        latest.c.decision,
        latest.c.decision_reason,
        latest.c.match_method,
        latest.c.rule_version,
        latest.c.decided_by,
        latest.c.decided_at,
    ).join(NewsQueryHit, NewsQueryHit.id == latest.c.query_hit_id)
    if instrument_ids is not None:
        stmt = stmt.where(NewsQueryHit.instrument_id.in_(list(instrument_ids)))
    return [DecisionAsOf(*row) for row in session.execute(stmt).all()]


def confirmed_pairs_asof(
    session: Session,
    *,
    asof: datetime,
    since: datetime,
    instrument_ids: Collection[int],
) -> set[tuple[int, int]]:
    """(instrument, article) pairs whose verdict at `asof` was CONFIRMED.

    Articles available in `(since, asof]` and stored by `asof`.
    """
    asof = ensure_utc(asof, field="asof")
    latest = _latest(asof=asof)
    stmt = (
        select(NewsQueryHit.instrument_id, NewsQueryHit.news_item_id)
        .select_from(latest)
        .join(NewsQueryHit, NewsQueryHit.id == latest.c.query_hit_id)
        .join(NewsItem, NewsItem.id == NewsQueryHit.news_item_id)
        .where(
            latest.c.decision == HitDecision.CONFIRMED,
            NewsQueryHit.instrument_id.in_(list(instrument_ids)),
            NewsItem.available_at > ensure_utc(since, field="since"),
            NewsItem.available_at <= asof,
            NewsItem.ingested_at <= asof,
        )
    )
    return {(i, n) for i, n in session.execute(stmt).all()}


def projection_drift(session: Session) -> tuple[int, int]:
    """(latest CONFIRMED verdicts with no mention, mentions with no such verdict).

    Both must be zero. Anything else means `news_mention` was written around
    `record_hits`, which is the drift the single write path exists to prevent.
    """
    latest = _latest()
    confirmed = (
        select(NewsQueryHit.news_item_id, NewsQueryHit.instrument_id)
        .join(latest, latest.c.query_hit_id == NewsQueryHit.id)
        .where(latest.c.decision == HitDecision.CONFIRMED)
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
    """How many hits hold each verdict now. For the CLI and the rollout."""
    latest = _latest()
    found = session.execute(select(latest.c.decision, func.count()).group_by(latest.c.decision))
    return {decision: int(n) for decision, n in found.all()}


def latest_available_at(
    session: Session,
    *,
    source: NewsSource | None = None,
    ingested_before: datetime | None = None,
) -> datetime | None:
    """The newest article we can see, for judging whether the pipe is flowing.

    This is the `source_asof` a `WallClockFreshnessRule` wants: news and social
    should keep arriving, so silence is itself the signal. Distinct from the
    fundamental rule, which asks when the source was last checked rather than
    how old the newest item is.

    There is no per-instrument form. The one that existed read `news_mention`,
    which holds today's verdicts, so asked about a past moment it answered
    with what we believe now.
    """
    stmt = select(func.max(NewsItem.available_at))
    if source is not None:
        stmt = stmt.where(NewsItem.source == source)
    if ingested_before is not None:
        bound = ensure_utc(ingested_before, field="ingested_before")
        # Both bounds. `ingested_at` is the sweep's start, so an article
        # published while it ran is stamped before it existed.
        stmt = stmt.where(NewsItem.ingested_at <= bound, NewsItem.available_at <= bound)
    return session.execute(stmt).scalar()


def earliest_available_at(
    session: Session,
    *,
    source: NewsSource | None = None,
    ingested_before: datetime | None = None,
) -> datetime | None:
    """How far back collection reaches, as far as the stored articles can say.

    Nothing before this was ever read, so a baseline window reaching further
    back is partly empty rather than quiet. It is the oldest article of any
    sweep, and an early sweep over a handful of names reaches as far as a full
    one, so for the other names it can overstate coverage by the difference.
    """
    stmt = select(func.min(NewsItem.available_at))
    if source is not None:
        stmt = stmt.where(NewsItem.source == source)
    if ingested_before is not None:
        stmt = stmt.where(
            NewsItem.ingested_at <= ensure_utc(ingested_before, field="ingested_before")
        )
    return session.execute(stmt).scalar()


class CoverageRow(NamedTuple):
    instrument_id: int
    source: NewsSource
    collector: str
    covered_from: datetime
    covered_to: datetime
    capped: bool


def record_coverage(session: Session, rows: Sequence[CoverageRow]) -> None:
    """Append what each sweep read, for instruments that still exist. Does not commit.

    An instrument removed while the sweep ran has nothing to record against.
    Joining at insert time rather than trusting the list the sweep started
    with keeps that from failing the whole run on a foreign key.
    """
    c = NewsSweepCoverage
    fields = list(CoverageRow._fields)
    for batch in bulk.batched(rows, columns=len(fields)):
        columns = c.__table__.c
        incoming = (
            select(*(literal(getattr(r, f), type_=columns[f].type).label(f) for f in fields))
            for r in batch
        )
        values = union_all(*incoming).subquery("incoming")
        chosen = select(*(values.c[f] for f in fields)).join(
            Instrument, Instrument.instrument_id == values.c.instrument_id
        )
        session.execute(insert(c).from_select(fields, chosen))


def coverage(
    session: Session,
    *,
    asof: datetime,
    start: datetime,
    end: datetime,
    collector: str,
    source: NewsSource | None = None,
) -> dict[int, list[tuple[datetime, datetime]]]:
    """Per instrument, the stretches of `[start, end]` a sweep recorded by `asof` had read.

    `collector` names whose sweeps count. Tests sweep the real master under
    names of their own, and what they claim to have read is not news.
    """
    asof = ensure_utc(asof, field="asof")
    start = ensure_utc(start, field="start")
    end = ensure_utc(end, field="end")
    c = NewsSweepCoverage
    stmt = select(c.instrument_id, c.covered_from, c.covered_to).where(
        c.collector == collector, c.recorded_at <= asof, c.covered_to > start, c.covered_from < end
    )
    if source is not None:
        stmt = stmt.where(c.source == source)
    out: dict[int, list[tuple[datetime, datetime]]] = {}
    for instrument_id, lo, hi in session.execute(stmt).all():
        out.setdefault(instrument_id, []).append((max(lo, start), min(hi, end)))
    return out


def confirmed_times(
    session: Session,
    *,
    asof: datetime,
    start: datetime,
    end: datetime,
    source: NewsSource | None = None,
) -> dict[int, list[datetime]]:
    """When each article confirmed as about an instrument became available.

    Articles available in `(start, end]` that had been stored by `asof` and
    whose verdict at `asof` was CONFIRMED. Each condition is a point-in-time
    bound: a later sweep, a later article and a later re-judgment are all
    invisible, so asking about the same moment gives the same answer after the
    rules have moved on. Times, not counts, so the caller can keep only the
    articles inside the stretches a sweep actually read.
    """
    asof = ensure_utc(asof, field="asof")
    latest = _latest(asof=asof)
    stmt = (
        select(NewsQueryHit.instrument_id, NewsItem.id, NewsItem.available_at)
        .select_from(latest)
        .join(NewsQueryHit, NewsQueryHit.id == latest.c.query_hit_id)
        .join(NewsItem, NewsItem.id == NewsQueryHit.news_item_id)
        .where(
            latest.c.decision == HitDecision.CONFIRMED,
            NewsItem.available_at > ensure_utc(start, field="start"),
            NewsItem.available_at <= ensure_utc(end, field="end"),
            NewsItem.ingested_at <= asof,
        )
        .distinct()
    )
    if source is not None:
        stmt = stmt.where(NewsItem.source == source)
    out: dict[int, list[datetime]] = {}
    for instrument_id, _, available_at in session.execute(stmt).all():
        out.setdefault(instrument_id, []).append(available_at)
    return out


def count_items(session: Session, *, source: NewsSource | None = None) -> int:
    stmt = select(func.count()).select_from(NewsItem)
    if source is not None:
        stmt = stmt.where(NewsItem.source == source)
    return int(session.execute(stmt).scalar_one())
