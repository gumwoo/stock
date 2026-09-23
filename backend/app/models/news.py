"""News articles and social posts, and which instruments they are about.

Two tables, not one. The same semiconductor article comes back from the query
for Samsung and from the query for SK Hynix; one table keyed by instrument
would store its text twice, and the sentiment scorer that lands next would then
pay to read the same words twice and could return two different scores for
them. Splitting the article from the mention costs fifteen lines of model code.

**`available_at` equals `published_at` here, and that is not a copy-paste of
the filing rule.** Filings get "next session open" because `rcept_dt` names a
day and not a moment, so the earliest defensible instant is the next open. An
article names a moment. Pushing a 10:00 article to the next morning would not
be conservative, it would be wrong — it would claim the market could not know
something it plainly could. The column still exists separately so that every
source table filters on the same name, and so a source whose publication and
availability genuinely differ can arrive later without changing readers.

**What `published_at` means is Naver's definition, not ours.** Naver documents
`pubDate` as the time the article was *supplied to Naver*, falling back to the
outlet's own time only for articles it never received. So it is at or after the
moment the outlet published. That direction is the safe one for point-in-time
work — we claim availability late — but it is not "the time the outlet
published", and nothing here should call it that.

**There is no historical backfill.** Collection starts the day it is switched
on, so news cannot support a backtest over any earlier period. This is the
concrete reason sentiment is an event overlay with its own half-life rather
than a weighted factor in the base score: the base score has ten years of
history behind it and this does not.

**Four tables, and only the last two are claims about a company.**

    news_item                what was published             a fact
    news_query_hit           which company's search found it a fact
    news_relevance_decision  is it about that company       every verdict, appended
    news_mention             it is, as of now                the latest, when CONFIRMED

A search result is not evidence that the article is about the company the
query named. The first rollout showed it within ten companies: `원림` is a
listed company and also the ordinary word for a garden, and seven of its
eighteen accepted articles were about gardens. So every hit is recorded with
its verdict — CONFIRMED, PENDING or REJECTED — and `news_mention` holds only
the CONFIRMED ones. Keeping the rejected and the undecided is what lets the
reject rate be computed again later, lets a later rule re-judge old hits, and
leaves the undecided ones for a model to judge instead of dropping them.

**Verdicts are appended, never updated.** The first version kept the verdict
on the hit row and overwrote it: a hit confirmed on 09/23 and re-judged PENDING
by a newer rule on 09/24 left only PENDING behind, and nothing could say it had
been a mention on 09/23. Worse, an unchanged verdict kept its time while its
snippet and rule version moved on, so a row could claim a verdict reached at
one moment from text read at another. That is the fundamental table's
`semantic_version` leak again, in a new place. So each verdict is its own row,
stamped by the database when written, carrying the exact text it read.

`news_mention` is the projection of the *latest* verdicts and has one writer,
`news_repo.record_hits`. It answers "what do we think now" and forgets a
withdrawn mention. Anything that must answer "what did we think then" — a
forward test, a reproduction, a candidate ranking at a past moment — reads
`news_repo.decisions_asof` instead, never the mention table.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk, IngestedAt


class NewsSource(StrEnum):
    """Where a text item came from."""

    NAVER_NEWS = "NAVER_NEWS"
    THREADS = "THREADS"
    REDDIT = "REDDIT"


class MatchMethod(StrEnum):
    """What actually tied an article to an instrument.

    A search returning a result is not evidence the article is about the
    company: Naver matches body text and related terms, so a query for a large
    holding pulls in articles that merely mention its sector. Recording the
    basis makes a wrong link findable later instead of invisible.
    """

    NAME = "NAME"
    """The instrument's registered name appears in the title or summary."""

    ALIAS = "ALIAS"
    """A registered alternate spelling appears."""

    SYMBOL = "SYMBOL"
    """The ticker appears. Rare in Korean coverage, which uses company names."""


class NewsItem(Base):
    """One article or post, stored once however many instruments it mentions."""

    __tablename__ = "news_item"

    id: Mapped[BigIntPk]

    source: Mapped[NewsSource] = mapped_column(
        Enum(NewsSource, name="news_source", native_enum=False, length=16), nullable=False
    )
    url_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        doc="sha256 of the canonical URL. Fixed width, so the unique index does "
        "not depend on how long a publisher's URLs happen to be.",
    )
    url: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
        doc="The publisher's own link where one was given, else the aggregator's.",
    )
    naver_url: Mapped[str | None] = mapped_column(
        String(1000), nullable=True, doc="Naver's mirror, when it differs from `url`."
    )
    publisher_host: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        doc="Host of the canonical URL. Free from parsing it, and the only "
        "handle a later filter on aggregators or low-quality outlets could use.",
    )
    title: Mapped[str] = mapped_column(
        String(500), nullable=False, doc="Markup and HTML entities already stripped."
    )
    summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="The source's snippet, stripped the same way."
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="Naver's `pubDate`: when the article was supplied to Naver, which is "
        "at or after the outlet published it. Not the outlet's own timestamp.",
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="When the market could read this. Equals published_at for news, "
        "which names a moment — unlike a filing date, which names only a day.",
    )
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        UniqueConstraint("source", "url_hash", name="uq_news_item_source_url_hash"),
        Index("ix_news_item_available", "available_at"),
    )

    def __repr__(self) -> str:
        return f"<NewsItem {self.source} {self.published_at} {self.title[:40]!r}>"


class NewsMention(Base):
    """One article's link to one instrument, with the basis for the link."""

    __tablename__ = "news_mention"

    id: Mapped[BigIntPk]
    news_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("news_item.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )

    matched_query: Mapped[str] = mapped_column(
        String(200), nullable=False, doc="The query that surfaced the article."
    )
    match_method: Mapped[MatchMethod] = mapped_column(
        Enum(MatchMethod, name="news_match_method", native_enum=False, length=16),
        nullable=False,
        doc="What was actually found in the text. A row exists only because this "
        "check passed, so a bad link can be traced to the rule that made it.",
    )
    ingested_at: Mapped[IngestedAt]

    __table_args__ = (
        UniqueConstraint("news_item_id", "instrument_id", name="uq_news_mention_item_instrument"),
        Index("ix_news_mention_instrument_item", "instrument_id", "news_item_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<NewsMention item={self.news_item_id} instrument={self.instrument_id} "
            f"via={self.match_method}>"
        )


class HitDecision(StrEnum):
    """Whether an article the search returned is about the company searched for."""

    CONFIRMED = "CONFIRMED"  # becomes a news_mention
    PENDING = "PENDING"  # the name is there, the company may not be; left for a model
    REJECTED = "REJECTED"  # the name is not there at all


class Decider(StrEnum):
    """Who reached the verdict. A later rule may overrule a rule; never a model."""

    RULE = "RULE"
    LLM = "LLM"


class NewsQueryHit(Base):
    """One company's search surfacing one article. A fact, never re-written.

    Keyed on the pair. The same article comes back every run while a window
    overlaps, and that is the same fact seen again, not a new one.
    """

    __tablename__ = "news_query_hit"

    id: Mapped[BigIntPk]
    news_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("news_item.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    matched_query: Mapped[str] = mapped_column(
        String(200), nullable=False, doc="The query that first surfaced the article."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        doc="When this pair was first seen.",
    )

    __table_args__ = (
        UniqueConstraint("news_item_id", "instrument_id", name="uq_news_query_hit_item_instrument"),
        Index("ix_news_query_hit_instrument", "instrument_id"),
    )

    def __repr__(self) -> str:
        return f"<NewsQueryHit item={self.news_item_id} instrument={self.instrument_id}>"


class NewsRelevanceDecision(Base):
    """One verdict on one hit, as reached at one moment. Append-only.

    A new row is written only when something about the verdict differs from
    the latest one: the decision, its reason, the rule version, who decided, or
    the text it read. A sweep that re-reads an article and agrees writes
    nothing, so the latest row at or before a moment is the verdict held then.

    `decided_at` is set by the database, not passed in, so no caller can date a
    verdict earlier than it was written. It is the insert time, and the sweep
    commits at its end, so a verdict becomes visible to other sessions a few
    minutes after its stamp. The same holds for `ingested_at` on every table
    here; a reproduction should not ask about a moment inside a running sweep.
    """

    __tablename__ = "news_relevance_decision"

    id: Mapped[BigIntPk]
    query_hit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("news_query_hit.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[HitDecision] = mapped_column(
        Enum(HitDecision, name="news_hit_decision", native_enum=False, length=12),
        nullable=False,
    )
    decision_reason: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        doc="Why, in a short machine-readable form: `absent`, `name`, "
        "`strong:title_lead`, `weak:실적+수주`, `context:none`, `legacy:mention`.",
    )
    match_method: Mapped[MatchMethod | None] = mapped_column(
        Enum(MatchMethod, name="news_match_method", native_enum=False, length=16),
        nullable=True,
        doc="How the name was found. Null when it was not found at all.",
    )
    matched_query: Mapped[str] = mapped_column(
        String(200), nullable=False, doc="The query whose result this verdict read."
    )
    snippet: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="The description that search returned, which is what this verdict "
        "read. Naver cuts the snippet around the query, so two companies' "
        "searches can return different text for one article, and "
        "`news_item.summary` keeps only the first. Null on verdicts carried over "
        "from before snippets were kept; those cannot be judged again from what "
        "was read.",
    )
    rule_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="The rule set that reached a RULE verdict, so a newer rule can find "
        "and re-judge exactly the hits an older one decided.",
    )
    decided_by: Mapped[Decider] = mapped_column(
        Enum(Decider, name="news_decider", native_enum=False, length=8),
        nullable=False,
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        nullable=False,
        doc="When the database recorded this verdict. `clock_timestamp`, not "
        "`now`, so two verdicts written in one transaction are still ordered.",
    )

    __table_args__ = (
        Index("ix_news_relevance_decision_hit_time", "query_hit_id", "decided_at", "id"),
        Index("ix_news_relevance_decision_time", "decided_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<NewsRelevanceDecision hit={self.query_hit_id} {self.decision} "
            f"({self.decision_reason}) at {self.decided_at}>"
        )


class NewsSweepCoverage(Base):
    """The stretch of time one sweep actually read for one company.

    A search returns the newest results first, one page of a hundred. For a
    quiet company that page reaches back past the watermark and the sweep has
    read everything since it: `[since, read_at]`. For a busy one the page runs
    out first and the sweep has read only `[oldest result, read_at]`; anything
    older in that sweep's range was never seen. The first full sweep hit that
    cap on 273 of 2,648 names, and a count of mentions over a window means
    nothing without knowing how much of the window was read.

    Append-only, one row per company per sweep that sent a request.
    """

    __tablename__ = "news_sweep_coverage"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[NewsSource] = mapped_column(
        Enum(NewsSource, name="news_source", native_enum=False, length=16), nullable=False
    )
    collector: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="The collector that swept, as `collector_run.source` names it. Tests "
        "sweep the real master under their own name, and a reader asks for the "
        "real collector's sweeps only.",
    )
    covered_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="The watermark, or the oldest result read when the page cap cut the sweep short.",
    )
    covered_to: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, doc="When the search was sent."
    )
    capped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, doc="The page cap or the budget cut this sweep short."
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        nullable=False,
        doc="When the database recorded it. A reader at a moment sees rows recorded by then.",
    )

    __table_args__ = (
        Index("ix_news_sweep_coverage_instrument_to", "instrument_id", "covered_to"),
        Index("ix_news_sweep_coverage_collector", "collector", "recorded_at"),
        Index("ix_news_sweep_coverage_recorded", "recorded_at"),
    )
