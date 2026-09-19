"""Access to the filing register.

Answers one question the value tables cannot: *did a qualifying report exist by
this date*. That is what separates "the market did not have this figure yet"
from "our value source does not reach back that far", and the two must not be
conflated — the first is a fact about the world, the second about our plumbing.

Every query here takes `ingested_before` for the same reason the value tables
do. The register is collected data like any other, and a filing backfilled
later carries an old `filed_at` that sails through any as-of filter. Without
the bound, re-running a stored backtest would reclassify its absences: the
value stays missing, but the *reason* changes, and a run that reports a
different provenance has not been reproduced.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Filing
from app.models.fundamental import FundamentalSource


class FilingRow(NamedTuple):
    instrument_id: int
    form: str
    filed_at: date
    period_of_report: date | None
    available_at: datetime
    accession: str
    source: FundamentalSource


def save_filings(session: Session, rows: Sequence[FilingRow]) -> int:
    """Insert filings, ignoring ones already recorded."""
    if not rows:
        return 0
    stmt = pg_insert(Filing).values([r._asdict() for r in rows])
    stmt = stmt.on_conflict_do_nothing(constraint="uq_filing_instrument_accession")
    return len(session.execute(stmt.returning(Filing.id)).scalars().all())


def form_family(form: str) -> str:
    """Strip the amendment suffix: 10-K/A belongs to the 10-K family.

    An amendment is a correction to a report, not a separate kind of report, so
    for "did a report covering this period exist" they count as one.
    """
    return form.split("/", 1)[0]


def covering_report_exists(
    session: Session,
    instrument_id: int,
    *,
    period_end: date,
    asof: datetime,
    families: Sequence[str] = ("10-K", "10-Q", "20-F", "40-F"),
    ingested_before: datetime | None = None,
) -> Filing | None:
    """The earliest report covering `period_end` that was available by `asof`.

    A report qualifies when its `period_of_report` matches the fiscal period in
    question. Matching is exact on the date, because a period end is a specific
    day and a report either covers it or does not.

    Args:
        ingested_before: restrict to filings the register already held then.
            A filing backfilled afterwards carries an old `filed_at` and
            would otherwise change how a reproduced run classifies absence.
    """
    stmt = (
        select(Filing)
        .where(
            Filing.instrument_id == instrument_id,
            Filing.period_of_report == period_end,
            Filing.available_at <= asof,
        )
        .order_by(Filing.filed_at)
    )
    if ingested_before is not None:
        stmt = stmt.where(Filing.ingested_at <= ingested_before)

    for filing in session.execute(stmt).scalars():
        if form_family(filing.form) in families:
            return filing
    return None


def register_start(
    session: Session,
    instrument_id: int,
    *,
    source: FundamentalSource | None = None,
    ingested_before: datetime | None = None,
) -> date | None:
    """Earliest filing date the register holds, i.e. how far back it can speak.

    Bounded by `ingested_before` so a reproduced run sees the register as it
    stood then. Otherwise a later backfill extends its apparent reach
    backwards and lets an old run start making claims it had declined to make.
    """
    stmt = select(func.min(Filing.filed_at)).where(Filing.instrument_id == instrument_id)
    if source is not None:
        stmt = stmt.where(Filing.source == source)
    if ingested_before is not None:
        stmt = stmt.where(Filing.ingested_at <= ingested_before)
    return session.execute(stmt).scalar()


def count_for(session: Session, instrument_id: int) -> int:
    stmt = select(func.count()).select_from(Filing).where(Filing.instrument_id == instrument_id)
    return int(session.execute(stmt).scalar() or 0)
