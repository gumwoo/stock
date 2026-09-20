"""Assembling a point-in-time fundamental snapshot.

The seam between the repository's PIT machinery and the pure engine. Every
lookup here goes through `fundamental_repo`, which means every value carries
its filing provenance and every absence carries its reason — and the engine
receives plain data that it could not have obtained any other way.

`ingested_before` and `source` are threaded through every call. They are the
two axes a backtest needs to pin, and a helper that quietly dropped them would
be the easiest place in the system to reintroduce a leak.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.engines.fundamental import REQUIRED_MONTHS, FundamentalSnapshot, ReportedValue
from app.models.fundamental import FundamentalSource
from app.repositories import fundamental_repo
from app.repositories.fundamental_repo import (
    FundamentalContext,
    RevisionPolicy,
)

logger = logging.getLogger(__name__)

# The unit each concept is reported in. Omitting this would let a USD revenue
# and a USD/shares EPS land in the same series — SEC nests facts by unit for
# exactly this reason.
CONCEPT_UNITS: dict[str, str] = {
    "Revenues": "USD",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "USD",
    "NetIncomeLoss": "USD",
    "OperatingIncomeLoss": "USD",
    "EarningsPerShareBasic": "USD/shares",
    "EarningsPerShareDiluted": "USD/shares",
    "Assets": "USD",
    "Liabilities": "USD",
    "StockholdersEquity": "USD",
    "CashAndCashEquivalentsAtCarryingValue": "USD",
}


def _lookup(
    session: Session,
    instrument_id: int,
    concept: str,
    *,
    asof: datetime,
    policy: RevisionPolicy,
    ingested_before: datetime | None,
    source: FundamentalSource | None,
) -> ReportedValue:
    """One concept, resolved to what was knowable at `asof`."""
    result = fundamental_repo.latest_value_as_of(
        session,
        instrument_id,
        concept=concept,
        unit=CONCEPT_UNITS[concept],
        asof=asof,
        months=REQUIRED_MONTHS[concept],
        policy=policy,
        ingested_before=ingested_before,
        source=source,
    )
    fact = result.fact
    return ReportedValue(
        concept=concept,
        value=fact.value if fact else None,
        outcome=result.outcome.value,
        explanation=result.explain(),
        filed_at=fact.filed_at if fact else None,
        form=fact.form if fact else None,
        period_end=fact.period_end if fact else None,
    )


def _at_period(
    session: Session,
    instrument_id: int,
    concept: str,
    period_end: date,
    *,
    asof: datetime,
    policy: RevisionPolicy,
    ingested_before: datetime | None,
    source: FundamentalSource | None,
) -> ReportedValue | None:
    """An instantaneous fact pinned to one balance date."""
    result = fundamental_repo.value_as_of(
        session,
        instrument_id,
        FundamentalContext(
            taxonomy="us-gaap",
            concept=concept,
            unit=CONCEPT_UNITS[concept],
            period_end=period_end,
            period_start=None,
        ),
        asof=asof,
        policy=policy,
        ingested_before=ingested_before,
        source=source,
    )
    if result.fact is None:
        return None
    return ReportedValue(
        concept=concept,
        value=result.fact.value,
        outcome=result.outcome.value,
        explanation=result.explain(),
        filed_at=result.fact.filed_at,
        form=result.fact.form,
        period_end=result.fact.period_end,
    )


def build_snapshot(
    session: Session,
    instrument_id: int,
    *,
    asof: datetime,
    price: float | None,
    currency: str,
    policy: RevisionPolicy = RevisionPolicy.AS_KNOWN_THEN,
    ingested_before: datetime | None = None,
    source: FundamentalSource | None = None,
) -> FundamentalSnapshot:
    """Resolve every concept the engine might use, as of one instant.

    **Balance items are pinned to the income statement's period.** A ratio that
    divides a flow by a stock has to take both from the same date, or it is not
    the ratio it claims to be. Left unpinned, Apple's ROE came out as FY2025
    annual net income over a balance dated nine months later — a number that
    corresponds to no actual period.

    The alignment is best-effort: if that period's balance was never tagged,
    the latest one is used and its own `period_end` records what happened, so
    the mismatch is visible rather than hidden.

    The prior-year block is fetched by asking the same question a year earlier
    rather than by reaching for the previous period row. That matters: a
    year-ago figure must be the one that was *knowable* a year ago, or a growth
    rate would compare a restated number against an original one and report a
    change that never happened.
    """
    values = {
        concept: _lookup(
            session,
            instrument_id,
            concept,
            asof=asof,
            policy=policy,
            ingested_before=ingested_before,
            source=source,
        )
        for concept in CONCEPT_UNITS
    }

    # Anchor on the annual income statement, then pull balances to match.
    anchor = values.get("NetIncomeLoss")
    if anchor is not None and anchor.period_end is not None:
        for concept, months in REQUIRED_MONTHS.items():
            if months is not None:
                continue
            aligned = _at_period(
                session,
                instrument_id,
                concept,
                anchor.period_end,
                asof=asof,
                policy=policy,
                ingested_before=ingested_before,
                source=source,
            )
            if aligned is not None:
                values[concept] = aligned

    a_year_earlier = asof - timedelta(days=365)
    prior = {
        concept: _lookup(
            session,
            instrument_id,
            concept,
            asof=a_year_earlier,
            policy=policy,
            ingested_before=ingested_before,
            source=source,
        )
        for concept in (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        )
    }

    return FundamentalSnapshot(
        instrument_id=instrument_id,
        asof=asof,
        price=price,
        currency=currency,
        values=values,
        prior_year=prior,
    )
