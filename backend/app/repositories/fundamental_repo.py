"""Point-in-time access to financial facts.

The selection rule, stated once:

    1. Match the reporting context exactly —
       (taxonomy, concept, unit, period_start, period_end, form).
    2. Within it, keep only revisions that had been filed by `asof`.
    3. Choose one, according to the strategy's revision policy.

Step 3 has two defensible answers and they are different strategies, so the
caller picks rather than the repository assuming:

    AS_KNOWN_THEN      the latest revision filed on or before asof — what a
                       market participant would have been looking at.
    AS_FIRST_REPORTED  the earliest filing of that period — what was originally
                       announced, ignoring later restatements.

Apple's FY2008 EPS makes the stakes concrete: 5.48 as first reported in 2009,
6.94 after the 2010 retrospective restatement. Asked at 2010-01-01 both
policies answer 5.48; asked at 2011-01-01 they answer 6.94 and 5.48
respectively. Getting this wrong does not raise an error, it just produces a
backtest that quietly knew the future.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import NamedTuple

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Fundamental
from app.models.fundamental import FiscalPeriod, FundamentalSource


class RevisionPolicy(StrEnum):
    """Which filing of a period to believe."""

    AS_KNOWN_THEN = "as-known-then"
    AS_FIRST_REPORTED = "as-first-reported"


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

    Conflict means the exact same context *and* the same filing, so there is
    nothing new to record — a re-collection, not a restatement. A restatement
    arrives under a different accession and inserts cleanly beside the old row.
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


def _context_filtered(
    instrument_id: int,
    concept: str,
    *,
    taxonomy: str | None = None,
    unit: str | None = None,
    form: str | None = None,
) -> Select[tuple[Fundamental]]:
    stmt = select(Fundamental).where(
        Fundamental.instrument_id == instrument_id,
        Fundamental.concept == concept,
    )
    if taxonomy is not None:
        stmt = stmt.where(Fundamental.taxonomy == taxonomy)
    if unit is not None:
        stmt = stmt.where(Fundamental.unit == unit)
    if form is not None:
        stmt = stmt.where(Fundamental.form == form)
    return stmt


def value_as_of(
    session: Session,
    instrument_id: int,
    concept: str,
    *,
    asof: datetime,
    period_end: date | None = None,
    taxonomy: str | None = None,
    unit: str | None = None,
    form: str | None = None,
    policy: RevisionPolicy = RevisionPolicy.AS_KNOWN_THEN,
    ingested_before: datetime | None = None,
) -> Fundamental | None:
    """The most recent fact for `concept` that was knowable at `asof`.

    Args:
        asof: the simulation instant. Facts become usable at `available_at`,
            which is the session after the filing date — not the filing date
            itself, since neither SEC nor DART publishes a time of day.
        period_end: pin to one fiscal period. Omitted, the newest period whose
            filing had arrived by `asof` is used, which is what a live scorer
            wants.
        policy: which revision of that period to believe.
        ingested_before: additionally restrict to rows our database already
            held at that instant, so a later backfill cannot change the answer
            to a question asked earlier.

    Returns None when nothing had been filed yet, which is a real answer: at
    the start of a backtest a company genuinely has no reported figures.
    """
    stmt = _context_filtered(instrument_id, concept, taxonomy=taxonomy, unit=unit, form=form).where(
        Fundamental.available_at <= asof
    )

    if period_end is not None:
        stmt = stmt.where(Fundamental.period_end == period_end)
    if ingested_before is not None:
        stmt = stmt.where(Fundamental.ingested_at <= ingested_before)

    if period_end is None:
        # Newest period first, then apply the revision policy within it.
        newest = (
            session.execute(stmt.order_by(Fundamental.period_end.desc()).limit(1)).scalars().first()
        )
        if newest is None:
            return None
        stmt = stmt.where(Fundamental.period_end == newest.period_end)

    order = (
        Fundamental.filed_at.desc()
        if policy is RevisionPolicy.AS_KNOWN_THEN
        else Fundamental.filed_at.asc()
    )
    return session.execute(stmt.order_by(order, Fundamental.id.desc()).limit(1)).scalars().first()


def revisions_of(
    session: Session,
    instrument_id: int,
    concept: str,
    period_end: date,
    *,
    taxonomy: str | None = None,
    unit: str | None = None,
) -> list[Fundamental]:
    """Every filed revision of one period, oldest filing first.

    Exists so a restatement can be shown rather than merely handled. The
    drawer can say "this figure was 5.48 when first reported and 6.94 after
    the 2010 restatement" instead of presenting one number as though it were
    the only one there had ever been.
    """
    stmt = _context_filtered(instrument_id, concept, taxonomy=taxonomy, unit=unit).where(
        Fundamental.period_end == period_end
    )
    return list(session.execute(stmt.order_by(Fundamental.filed_at)).scalars().all())


def latest_filing_date(
    session: Session, instrument_id: int, *, source: FundamentalSource | None = None
) -> date | None:
    """Filing date of the newest fact stored for an instrument.

    Used for freshness. Note that this answers "how old is the data", which for
    fundamentals is the *wrong* question on its own — a quarterly report is old
    by nature. Availability is decided by how recently the source was checked,
    which `collector_run` answers.
    """
    stmt = select(func.max(Fundamental.filed_at)).where(Fundamental.instrument_id == instrument_id)
    if source is not None:
        stmt = stmt.where(Fundamental.source == source)
    return session.execute(stmt).scalar()


def concepts_for(session: Session, instrument_id: int) -> list[str]:
    """Distinct concepts stored for an instrument."""
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
