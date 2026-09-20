"""Point-in-time access to financial facts.

The selection rule, stated once:

    1. Match the reporting context exactly —
       (taxonomy, concept, unit, period_start, period_end, form).
    2. Within it, keep only revisions that had been filed by `asof`.
    3. Choose one, according to the strategy's revision policy.

**`period_start` is part of the context, not an optional refinement.** Apple's
own filings contain 92 cases where it is the only thing separating two facts:
a 10-Q dated 2026-07-31 reports net income of $29.8bn for the three months to
2026-06-27 and $101.5bn for the nine months to the same date. Identical
concept, unit, period_end, form and filing. Selecting without `period_start`
picks one by row id, and a valuation built on the wrong one is out by 3.4x.

**A missing answer has two very different meanings.** Either the figure had not
been filed yet, or our source does not reach back that far. SEC's XBRL
companyfacts only begins around mid-2009 — Apple's earliest fact of any kind is
filed 2009-07-22 — because XBRL tagging was phased in from June 2009 rather
than applied retrospectively. Apple's FY2008 10-K was filed 2008-11-05 and the
market knew its EPS from that date, but companyfacts holds no record of it.
Reporting that as "not filed yet" would assert something false about what the
market knew, so the two outcomes are returned distinctly and the caller is told
where coverage begins.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import NamedTuple

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Filing, Fundamental
from app.models.fundamental import FiscalPeriod, FundamentalSource
from app.repositories import filing_repo


class RevisionPolicy(StrEnum):
    """Which filing of a period to believe.

    AS_KNOWN_THEN is what a market participant would have been looking at.

    FIRST_OBSERVED_IN_SOURCE is named for what it can actually promise: the
    earliest filing *this source carries*, which is the original announcement
    only where source coverage reaches back that far. For SEC XBRL it does not
    before mid-2009, so calling it "as first reported" would overstate it.
    """

    AS_KNOWN_THEN = "as-known-then"
    FIRST_OBSERVED_IN_SOURCE = "first-observed-in-source"


class FactOutcome(StrEnum):
    """Why a lookup returned what it did.

    Three distinct kinds of absence, ordered from weakest to strongest claim:

    SOURCE_COVERAGE_UNAVAILABLE
        The value source does not reach this era at all. Says nothing about
        the world.

    NO_OBSERVATION_IN_SOURCE
        The source covers this era but holds no value for this context. Still
        says nothing about the world — the figure may well have been published
        in a filing our source never tagged.

    NOT_YET_FILED
        A claim about the world, and only returned when the filing register
        positively shows no qualifying report had been submitted by then.

    The middle state exists because the earlier design lacked it and therefore
    over-claimed: an instrument-wide coverage start of 2009-07-22 made a
    2009-08-01 lookup for FY2008 EPS report NOT_YET_FILED, when that report had
    in fact been filed on 2008-11-05.
    """

    FOUND = "FOUND"
    SOURCE_COVERAGE_UNAVAILABLE = "SOURCE_COVERAGE_UNAVAILABLE"
    NO_OBSERVATION_IN_SOURCE = "NO_OBSERVATION_IN_SOURCE"
    NOT_YET_FILED = "NOT_YET_FILED"


@dataclass(frozen=True, slots=True)
class FundamentalContext:
    """The full identity of a reported series.

    Every field participates. Two facts differing in any one of them are
    different measurements and must never be treated as revisions of each other.
    """

    taxonomy: str
    concept: str
    unit: str
    period_end: date
    period_start: date | None = None
    form: str | None = None


@dataclass(frozen=True, slots=True)
class FactLookup:
    """The result of a point-in-time question, including why it is empty.

    `coverage_start` is the earliest filing date this source holds for the
    instrument. When `asof` precedes it, absence says nothing about what the
    market knew — only that we cannot see that far back.
    """

    outcome: FactOutcome
    fact: Fundamental | None = None
    coverage_start: date | None = None
    covering_filing: Filing | None = None

    @property
    def value(self) -> Decimal | None:
        return self.fact.value if self.fact else None

    @property
    def usable(self) -> bool:
        return self.outcome is FactOutcome.FOUND

    def explain(self) -> str:
        """A sentence the UI can show instead of an empty cell."""
        if self.outcome is FactOutcome.FOUND and self.fact is not None:
            return f"{self.fact.value} as filed {self.fact.filed_at} ({self.fact.form})"

        if self.outcome is FactOutcome.NOT_YET_FILED:
            return "no report covering this period had been filed by this date"

        if self.outcome is FactOutcome.SOURCE_COVERAGE_UNAVAILABLE:
            if self.coverage_start is None:
                # No facts at all for this instrument — every Korean listing,
                # until the DART collector lands. Printing "begins None" would
                # be worse than saying nothing.
                return (
                    "this source holds no data at all for this instrument, so its "
                    "absence says nothing about what the market knew"
                )
            return (
                f"outside source coverage — this source begins {self.coverage_start}, "
                "so absence here says nothing about what the market knew"
            )

        if self.covering_filing is not None:
            return (
                f"a {self.covering_filing.form} covering this period was filed "
                f"{self.covering_filing.filed_at}, but our value source carries no "
                "figure for it — the market had it and we cannot read it"
            )
        return (
            "the source covers this era but holds no value for this context; "
            "absence is not evidence the figure was unpublished"
        )


class FundamentalRow(NamedTuple):
    """One fact ready for persistence."""

    instrument_id: int
    taxonomy: str
    concept: str
    unit: str
    period_start: date | None
    period_end: date
    fiscal_year: int | None
    fiscal_period: FiscalPeriod
    form: str
    value: Decimal
    filed_at: date
    available_at: datetime
    accession: str | None
    source: FundamentalSource
    frame: str | None = None


def save_facts(session: Session, rows: Sequence[FundamentalRow]) -> int:
    """Insert facts, ignoring ones already stored. Returns rows written.

    Conflict means the same context *and* the same filing, so there is nothing
    new to record — a re-collection, not a restatement. A restatement arrives
    under a different accession and inserts cleanly beside the old row.
    """
    if not rows:
        return 0

    stmt = pg_insert(Fundamental).values([r._asdict() for r in rows])
    stmt = stmt.on_conflict_do_nothing(constraint="uq_fundamental_context_filing")
    # RETURNING rather than rowcount: with ON CONFLICT DO NOTHING the driver
    # reports -1 for a multi-values insert, so the only reliable count is the
    # ids actually produced.
    inserted = session.execute(stmt.returning(Fundamental.id)).scalars().all()
    return len(inserted)


def coverage_start(
    session: Session,
    instrument_id: int,
    *,
    source: FundamentalSource | None = None,
    ingested_before: datetime | None = None,
) -> date | None:
    """Earliest filing date this source holds for the instrument.

    The boundary below which absence is uninformative. For SEC this lands
    around mid-2009 regardless of how old the company is, because XBRL tagging
    was phased in from June 2009 and not applied to earlier filings.

    Bounded by `ingested_before` so a reproduced run sees the boundary as it
    stood then, not as later backfills have extended it.
    """
    stmt = select(func.min(Fundamental.filed_at)).where(Fundamental.instrument_id == instrument_id)
    if source is not None:
        stmt = stmt.where(Fundamental.source == source)
    if ingested_before is not None:
        stmt = stmt.where(Fundamental.ingested_at <= ingested_before)
    return session.execute(stmt).scalar()


def _context_filtered(
    instrument_id: int,
    context: FundamentalContext,
) -> Select[tuple[Fundamental]]:
    """Restrict to exactly one reported series.

    `period_start` is matched with IS NOT DISTINCT FROM so that NULL — which
    marks an instantaneous fact such as a balance — matches NULL rather than
    matching nothing.
    """
    stmt = select(Fundamental).where(
        Fundamental.instrument_id == instrument_id,
        Fundamental.taxonomy == context.taxonomy,
        Fundamental.concept == context.concept,
        Fundamental.unit == context.unit,
        Fundamental.period_end == context.period_end,
        Fundamental.period_start.is_not_distinct_from(context.period_start),
    )
    if context.form is not None:
        stmt = stmt.where(Fundamental.form == context.form)
    return stmt


def _empty_result(
    session: Session,
    instrument_id: int,
    asof: datetime,
    source: FundamentalSource | None,
    period_end: date | None = None,
    ingested_before: datetime | None = None,
) -> FactLookup:
    """Classify an absent value into the weakest claim the evidence supports.

    The order matters. Only after ruling out both kinds of source limitation,
    and only with a filing register that positively shows no covering report,
    may this assert NOT_YET_FILED — a statement about the world rather than
    about our plumbing.

    Every query below carries `ingested_before`. Filtering the fact lookup
    alone is not enough: the value stays absent either way, but a backfilled
    filing would change the *reason*, and a reproduced run that reports a
    different provenance has not been reproduced.
    """
    begins = coverage_start(session, instrument_id, source=source, ingested_before=ingested_before)

    if begins is None or asof.date() < begins:
        return FactLookup(FactOutcome.SOURCE_COVERAGE_UNAVAILABLE, coverage_start=begins)

    if period_end is None:
        # Without a period there is nothing to look up in the register.
        return FactLookup(FactOutcome.NO_OBSERVATION_IN_SOURCE, coverage_start=begins)

    register_begins = filing_repo.register_start(
        session, instrument_id, ingested_before=ingested_before
    )
    if register_begins is None or asof.date() < register_begins:
        # The register cannot speak to this date either, so no claim is made.
        return FactLookup(FactOutcome.NO_OBSERVATION_IN_SOURCE, coverage_start=begins)

    covering = filing_repo.covering_report_exists(
        session,
        instrument_id,
        period_end=period_end,
        asof=asof,
        ingested_before=ingested_before,
    )
    if covering is not None:
        # The report existed; our value source simply never tagged it.
        return FactLookup(
            FactOutcome.NO_OBSERVATION_IN_SOURCE,
            coverage_start=begins,
            covering_filing=covering,
        )

    return FactLookup(FactOutcome.NOT_YET_FILED, coverage_start=begins)


def value_as_of(
    session: Session,
    instrument_id: int,
    context: FundamentalContext,
    *,
    asof: datetime,
    policy: RevisionPolicy = RevisionPolicy.AS_KNOWN_THEN,
    ingested_before: datetime | None = None,
    source: FundamentalSource | None = None,
) -> FactLookup:
    """The fact for `context` that was knowable at `asof`.

    Args:
        asof: the simulation instant. Facts become usable at `available_at`,
            the session after the filing date — neither SEC nor DART publishes
            a time of day, so the filing date itself is not a safe boundary.
        policy: which revision of the period to believe.
        ingested_before: additionally restrict to rows our database already
            held then, so a later backfill cannot change an earlier answer.

    Returns a `FactLookup` rather than a bare value, because an empty result
    means either "not filed yet" or "before this source begins", and conflating
    them lets a coverage gap masquerade as market ignorance.
    """
    stmt = _context_filtered(instrument_id, context).where(Fundamental.available_at <= asof)
    if ingested_before is not None:
        stmt = stmt.where(Fundamental.ingested_at <= ingested_before)
    if source is not None:
        stmt = stmt.where(Fundamental.source == source)

    order = (
        Fundamental.filed_at.desc()
        if policy is RevisionPolicy.AS_KNOWN_THEN
        else Fundamental.filed_at.asc()
    )
    fact = session.execute(stmt.order_by(order, Fundamental.id.desc()).limit(1)).scalars().first()

    if fact is not None:
        return FactLookup(FactOutcome.FOUND, fact)
    return _empty_result(session, instrument_id, asof, source, context.period_end, ingested_before)


def latest_value_as_of(
    session: Session,
    instrument_id: int,
    *,
    concept: str,
    unit: str,
    asof: datetime,
    taxonomy: str = "us-gaap",
    months: int | None = None,
    policy: RevisionPolicy = RevisionPolicy.AS_KNOWN_THEN,
    ingested_before: datetime | None = None,
    source: FundamentalSource | None = None,
) -> FactLookup:
    """Most recent period of `concept` that was knowable at `asof`.

    What a live scorer wants: not a named period, but whichever one is latest.

    Args:
        months: required duration in months for period facts, so a quarterly
            figure is never silently compared against a year-to-date one. Pass
            None for instantaneous facts, which carry no start date.
        ingested_before: restrict to rows the database already held then.
            **Not optional in a backtest.** Without it this helper reaches past
            the transaction-time axis: a filing backfilled in 2026 carries a
            2025 filing date, satisfies `available_at <= asof`, and silently
            changes the result of a backtest that ran before the backfill
            existed. The period-selection query below applies it too, because
            picking the newest period from rows we did not have then would be
            the same leak one step earlier.
        source: restrict to one provider. A historical backtest should ask for
            SEC only — the yfinance fallback carries no filing dates and cannot
            support a point-in-time claim.
    """
    stmt = select(Fundamental).where(
        Fundamental.instrument_id == instrument_id,
        Fundamental.taxonomy == taxonomy,
        Fundamental.concept == concept,
        Fundamental.unit == unit,
        Fundamental.available_at <= asof,
    )
    if ingested_before is not None:
        stmt = stmt.where(Fundamental.ingested_at <= ingested_before)
    if source is not None:
        stmt = stmt.where(Fundamental.source == source)

    if months is None:
        stmt = stmt.where(Fundamental.period_start.is_(None))
    else:
        # Month lengths vary and fiscal calendars drift, so the window is
        # generous. It still separates a 3-month figure from a 9-month one.
        low, high = months * 28, months * 31 + 10
        span = Fundamental.period_end - Fundamental.period_start
        stmt = stmt.where(Fundamental.period_start.is_not(None), span.between(low, high))

    newest = (
        session.execute(stmt.order_by(Fundamental.period_end.desc()).limit(1)).scalars().first()
    )

    if newest is None:
        return _empty_result(session, instrument_id, asof, source, None, ingested_before)

    return value_as_of(
        session,
        instrument_id,
        FundamentalContext(
            taxonomy=taxonomy,
            concept=concept,
            unit=unit,
            period_end=newest.period_end,
            period_start=newest.period_start,
        ),
        asof=asof,
        policy=policy,
        ingested_before=ingested_before,
        source=source,
    )


def previous_annual_fact(
    session: Session,
    instrument_id: int,
    *,
    concept: str,
    unit: str,
    before_period_end: date,
    asof: datetime,
    taxonomy: str = "us-gaap",
    months: int = 12,
    policy: RevisionPolicy = RevisionPolicy.AS_KNOWN_THEN,
    ingested_before: datetime | None = None,
    source: FundamentalSource | None = None,
) -> FactLookup:
    """The annual period immediately before `before_period_end`.

    Steps back one *fiscal period*, not one calendar year. Subtracting 365 days
    from `asof` and asking what was latest then looks equivalent and is not,
    because it conflates two unrelated things: when a period ended, and when
    its report happened to become readable.

    Both ways of getting it wrong were observable in Apple's own data:

    * FY2024 became usable on 2024-11-04. Scoring on 2025-11-03 put
      `asof - 365` at 2024-11-03, one day short, so the lookup fell through to
      FY2023 and reported a 728-day change as year-on-year growth — +8.6%
      where the real figure was +6.4%. Both are perfectly plausible numbers.

    * Asking as of a year ago also refuses to see any revision published
      since. Apple restated FY2024 revenue in the FY2025 10-K filed
      2025-10-31; a scorer running in November 2025 under AS_KNOWN_THEN should
      use that, because the market had it.

    So the period is chosen by fiscal calendar, and only then is the revision
    chosen by the *current* `asof`.
    """
    low, high = months * 28, months * 31 + 10
    span = Fundamental.period_end - Fundamental.period_start

    prior_end = session.execute(
        select(func.max(Fundamental.period_end)).where(
            Fundamental.instrument_id == instrument_id,
            Fundamental.taxonomy == taxonomy,
            Fundamental.concept == concept,
            Fundamental.unit == unit,
            Fundamental.period_end < before_period_end,
            Fundamental.period_start.is_not(None),
            span.between(low, high),
            Fundamental.available_at <= asof,
            *([Fundamental.ingested_at <= ingested_before] if ingested_before else []),
            *([Fundamental.source == source] if source else []),
        )
    ).scalar()

    if prior_end is None:
        return _empty_result(session, instrument_id, asof, source, None, ingested_before)

    prior_start = session.execute(
        select(func.max(Fundamental.period_start)).where(
            Fundamental.instrument_id == instrument_id,
            Fundamental.taxonomy == taxonomy,
            Fundamental.concept == concept,
            Fundamental.unit == unit,
            Fundamental.period_end == prior_end,
            Fundamental.available_at <= asof,
        )
    ).scalar()

    return value_as_of(
        session,
        instrument_id,
        FundamentalContext(
            taxonomy=taxonomy,
            concept=concept,
            unit=unit,
            period_end=prior_end,
            period_start=prior_start,
        ),
        asof=asof,
        policy=policy,
        ingested_before=ingested_before,
        source=source,
    )


def revisions_of(
    session: Session,
    instrument_id: int,
    context: FundamentalContext,
) -> list[Fundamental]:
    """Every filed revision of one series, oldest filing first.

    Exists so a restatement can be shown rather than merely handled — "5.48
    when first tagged, 6.94 after the 2010 amendment" is more honest than one
    number presented as the only one there has ever been.
    """
    return list(
        session.execute(_context_filtered(instrument_id, context).order_by(Fundamental.filed_at))
        .scalars()
        .all()
    )


def latest_filing_date(
    session: Session, instrument_id: int, *, source: FundamentalSource | None = None
) -> date | None:
    """Filing date of the newest fact stored for an instrument.

    Note this answers "how old is the data", which for fundamentals is the
    wrong question on its own — a quarterly report is old by nature.
    Availability is decided by how recently the source was checked, which
    `collector_run` answers.
    """
    stmt = select(func.max(Fundamental.filed_at)).where(Fundamental.instrument_id == instrument_id)
    if source is not None:
        stmt = stmt.where(Fundamental.source == source)
    return session.execute(stmt).scalar()


def concepts_for(session: Session, instrument_id: int) -> list[str]:
    stmt = (
        select(Fundamental.concept)
        .where(Fundamental.instrument_id == instrument_id)
        .distinct()
        .order_by(Fundamental.concept)
    )
    return list(session.execute(stmt).scalars().all())


def count_for(session: Session, instrument_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(Fundamental)
        .where(Fundamental.instrument_id == instrument_id)
    )
    return int(session.execute(stmt).scalar() or 0)
