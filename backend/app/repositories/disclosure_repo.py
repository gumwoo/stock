"""Event disclosures: stored once by receipt number, read as they stood at a moment."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date, datetime
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.clock import ensure_utc
from app.models.disclosure import Disclosure
from app.repositories import bulk


class DisclosureRow(NamedTuple):
    instrument_id: int
    rcept_no: str
    report_nm: str
    pblntf_ty: str
    filer: str | None
    filed_on: date
    available_at: datetime


def save_disclosures(session: Session, rows: Sequence[DisclosureRow]) -> int:
    """Insert, ignoring receipts already stored. Does not commit. Returns rows new."""
    written = 0
    for batch in bulk.batched(rows, columns=len(DisclosureRow._fields)):
        stmt = pg_insert(Disclosure).values([r._asdict() for r in batch])
        stmt = stmt.on_conflict_do_nothing(index_elements=["rcept_no"])
        written += len(session.execute(stmt.returning(Disclosure.id)).scalars().all())
    return written


class DisclosureAsOf(NamedTuple):
    id: int
    instrument_id: int
    report_nm: str
    available_at: datetime


def disclosures_asof(
    session: Session,
    *,
    asof: datetime,
    since: datetime,
    instrument_ids: Collection[int],
) -> list[DisclosureAsOf]:
    """Disclosures usable at `asof`: available by then and stored by then."""
    asof = ensure_utc(asof, field="asof")
    d = Disclosure
    stmt = select(d.id, d.instrument_id, d.report_nm, d.available_at).where(
        d.instrument_id.in_(list(instrument_ids)),
        d.available_at > ensure_utc(since, field="since"),
        d.available_at <= asof,
        d.ingested_at <= asof,
    )
    return [DisclosureAsOf(*row) for row in session.execute(stmt).all()]


def filed_between(
    session: Session,
    *,
    first: date,
    before: date,
    stored_by: datetime,
    instrument_ids: Collection[int],
) -> list[DisclosureAsOf]:
    """Disclosures filed on a day in [first, before) and stored by `stored_by`.

    For a reader before the open: a filing dated an earlier calendar day was
    public by midnight of that day, so it is known that morning even though its
    `available_at` — the next session's open, because the receipt carries no
    time — is still a few minutes away. The overlay keeps its own, stricter rule.
    """
    d = Disclosure
    stmt = select(d.id, d.instrument_id, d.report_nm, d.available_at).where(
        d.instrument_id.in_(list(instrument_ids)),
        d.filed_on >= first,
        d.filed_on < before,
        d.ingested_at <= ensure_utc(stored_by, field="stored_by"),
    )
    return [DisclosureAsOf(*row) for row in session.execute(stmt).all()]
