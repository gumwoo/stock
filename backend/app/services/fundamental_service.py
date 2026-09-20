"""Assembling a point-in-time fundamental snapshot.

The seam between the repository's PIT machinery and the pure engine. Every
lookup here goes through `fundamental_repo`, so every value carries its filing
provenance and every absence carries its reason, and the engine receives plain
data it could not have obtained any other way.

**Everything is anchored to one fiscal period.** Resolving each concept
independently looks harmless and is not: a ratio built from two different
periods is not a wrong number, it is a number describing no period that ever
existed, and nothing about it looks unusual. Apple's own filings produce this
today — at a 2026 as-of date the `Revenues` tag still reports 2018-09-29,
because Apple moved to `RevenueFromContractWithCustomerExcludingAssessedTax`
when it adopted ASC 606, while every other concept reports 2025-09-27. An
operating margin taken from those two would divide FY2025 income by FY2018
revenue and read as a plausible percentage.

So an anchor period is chosen first, and every input is then looked up *at that
period*. A concept the anchor period never tagged is reported absent rather
than silently filled from another year.

`ingested_before` and `source` are threaded through every call. They are the
two axes a backtest needs to pin, and a helper that quietly dropped them would
be the easiest place in the system to reintroduce a leak.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.engines.fundamental import REQUIRED_MONTHS, FundamentalSnapshot, ReportedValue
from app.models.fundamental import FundamentalSource
from app.repositories import fundamental_repo
from app.repositories.fundamental_repo import FundamentalContext, RevisionPolicy

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

# Preference order for choosing the anchor. Net income is tagged by essentially
# every filer in every period, which makes it the most reliable spine; the
# others are fallbacks for filers that do not.
ANCHOR_CANDIDATES = (
    "NetIncomeLoss",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "EarningsPerShareBasic",
)


def _absent(concept: str, reason: str) -> ReportedValue:
    return ReportedValue(
        concept=concept,
        value=None,
        outcome="NO_OBSERVATION_IN_SOURCE",
        explanation=reason,
    )


def _from_lookup(concept: str, result: fundamental_repo.FactLookup) -> ReportedValue:
    fact = result.fact
    return ReportedValue(
        concept=concept,
        value=fact.value if fact else None,
        outcome=result.outcome.value,
        explanation=result.explain(),
        filed_at=fact.filed_at if fact else None,
        form=fact.form if fact else None,
        period_end=fact.period_end if fact else None,
        period_start=fact.period_start if fact else None,
    )


def _latest(
    session: Session,
    instrument_id: int,
    concept: str,
    *,
    asof: datetime,
    policy: RevisionPolicy,
    ingested_before: datetime | None,
    source: FundamentalSource | None,
) -> ReportedValue:
    """Whatever period is most recent for this concept."""
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
    return _from_lookup(concept, result)


def _at_anchor(
    session: Session,
    instrument_id: int,
    concept: str,
    *,
    period_start: date | None,
    period_end: date,
    asof: datetime,
    policy: RevisionPolicy,
    ingested_before: datetime | None,
    source: FundamentalSource | None,
) -> ReportedValue:
    """This concept at exactly the anchor period, or a reasoned absence."""
    result = fundamental_repo.value_as_of(
        session,
        instrument_id,
        FundamentalContext(
            taxonomy="us-gaap",
            concept=concept,
            unit=CONCEPT_UNITS[concept],
            period_end=period_end,
            period_start=period_start,
        ),
        asof=asof,
        policy=policy,
        ingested_before=ingested_before,
        source=source,
    )
    if result.fact is None:
        return _absent(
            concept,
            f"not tagged for the fiscal period ending {period_end}; using another "
            "period would produce a ratio describing no real period",
        )
    return _from_lookup(concept, result)


def _previous_annual(
    session: Session,
    instrument_id: int,
    concept: str,
    *,
    before_period_end: date,
    asof: datetime,
    policy: RevisionPolicy,
    ingested_before: datetime | None,
    source: FundamentalSource | None,
) -> ReportedValue:
    """The prior fiscal year, chosen by calendar and read at the current asof.

    Stepping back one fiscal period rather than 365 days matters twice over.

    The period must be the one immediately before the anchor. Apple's FY2024
    became usable on 2024-11-04, so a score run on 2025-11-03 put `asof - 365`
    one day short of it and fell through to FY2023 — reporting a 728-day change
    as year-on-year growth, +8.6% where the truth was +6.4%. Neither figure
    looks wrong on its own.

    The revision must be the one knowable *now*. Asking as of a year ago also
    hides every restatement published since; Apple restated FY2024 revenue in
    the FY2025 10-K, and under AS_KNOWN_THEN a scorer running afterwards should
    use it, because the market had it.
    """
    result = fundamental_repo.previous_annual_fact(
        session,
        instrument_id,
        concept=concept,
        unit=CONCEPT_UNITS[concept],
        before_period_end=before_period_end,
        asof=asof,
        policy=policy,
        ingested_before=ingested_before,
        source=source,
    )
    return _from_lookup(concept, result)


def _choose_anchor(
    session: Session,
    instrument_id: int,
    *,
    asof: datetime,
    policy: RevisionPolicy,
    ingested_before: datetime | None,
    source: FundamentalSource | None,
) -> ReportedValue | None:
    """The fiscal period every input will be pinned to."""
    for concept in ANCHOR_CANDIDATES:
        candidate = _latest(
            session,
            instrument_id,
            concept,
            asof=asof,
            policy=policy,
            ingested_before=ingested_before,
            source=source,
        )
        if candidate.present and candidate.period_end is not None:
            return candidate
    return None


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
    """Resolve every concept the engine might use, all at one fiscal period.

    The prior-year block is fetched by asking the same question a year earlier
    rather than by reaching for the previous period row. That matters: a
    year-ago figure must be the one that was *knowable* a year ago, or a growth
    rate would compare a restated number against an original one and report a
    change that never happened.
    """
    anchor = _choose_anchor(
        session,
        instrument_id,
        asof=asof,
        policy=policy,
        ingested_before=ingested_before,
        source=source,
    )

    if anchor is None or anchor.period_end is None:
        # Nothing to anchor to. Report each concept's own absence, which
        # carries the repository's reason for why it is missing.
        values = {
            concept: _latest(
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
        return FundamentalSnapshot(
            instrument_id=instrument_id,
            asof=asof,
            price=price,
            currency=currency,
            values=values,
            anchor_period_end=None,
        )

    # Duration facts share the anchor's span; instantaneous ones are measured
    # at its end date and carry no start at all.
    values = {
        concept: _at_anchor(
            session,
            instrument_id,
            concept,
            period_start=anchor.period_start if months is not None else None,
            period_end=anchor.period_end,
            asof=asof,
            policy=policy,
            ingested_before=ingested_before,
            source=source,
        )
        for concept, months in REQUIRED_MONTHS.items()
    }

    prior = {
        concept: _previous_annual(
            session,
            instrument_id,
            concept,
            before_period_end=anchor.period_end,
            asof=asof,
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
        anchor_period_end=anchor.period_end,
    )
