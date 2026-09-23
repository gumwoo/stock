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

**Three layers, and only the last is a claim about a company.**

    news_item       what was published            a fact
    news_query_hit  which query surfaced it       a fact, plus a verdict
    news_mention    it is about this company      the verdict, when CONFIRMED

A search result is not evidence that the article is about the company the
query named. The first rollout showed it within ten companies: `원림` is a
listed company and also the ordinary word for a garden, and seven of its
eighteen accepted articles were about gardens. So every hit is recorded with
its verdict — CONFIRMED, PENDING or REJECTED — and `news_mention` holds only
the CONFIRMED ones. Keeping the rejected and the undecided is what lets the
reject rate be computed again later, lets a later rule re-judge old hits, and
leaves the undecided ones for a model to judge instead of dropping them.

`news_mention` is a projection of `news_query_hit` and has one writer,
`news_repo.record_hits`. Two tables that both say "confirmed" and were written
separately would drift.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
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
    """One article surfaced by one company's search, and the verdict on it.

    Keyed on the pair, and re-judged in place. The same article comes back
    every run while a window overlaps or a sweep is re-read after a PARTIAL, so
    one row per run would grow without bound and say nothing new.

    `decided_at` moves only when the verdict changes. A PENDING hit confirmed by
    a model next month did not exist as a confirmation today, and a reader
    reconstructing an earlier moment must be able to tell.
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
        String(200), nullable=False, doc="The query that surfaced the article, most recently."
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
    snippet: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="The description this search returned, which is what the verdict "
        "read. Naver cuts the snippet around the query, so two companies' "
        "searches can return different text for one article, and "
        "`news_item.summary` keeps only the first. Null on hits stored before "
        "the column existed; those cannot be judged again from what was read.",
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        doc="When this pair was first seen.",
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="When the current verdict was reached. Unchanged by a re-judgment "
        "that reaches the same verdict.",
    )

    __table_args__ = (
        UniqueConstraint("news_item_id", "instrument_id", name="uq_news_query_hit_item_instrument"),
        Index("ix_news_query_hit_instrument_decision", "instrument_id", "decision"),
        Index("ix_news_query_hit_decision_rule", "decision", "decided_by", "rule_version"),
    )

    def __repr__(self) -> str:
        return (
            f"<NewsQueryHit item={self.news_item_id} instrument={self.instrument_id} "
            f"{self.decision} ({self.decision_reason})>"
        )
