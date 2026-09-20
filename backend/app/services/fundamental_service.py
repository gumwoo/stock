"""Assembling a point-in-time fundamental snapshot.

The seam between the repository's PIT machinery and the pure engine. Every
lookup goes through `fundamental_repo`, so every value carries its filing
provenance and every absence carries its reason, and the engine receives plain
data it could not have obtained any other way.

**Everything is anchored to one fiscal period.** Resolving each concept
independently looks harmless and is not: a ratio built from two periods is not
a wrong number, it is a number describing no period that ever existed, and
nothing about it looks unusual. Apple's filings produce this today — at a 2026
as-of date the `Revenues` tag still reports 2018-09-29, because Apple moved to
`RevenueFromContractWithCustomerExcludingAssessedTax` under ASC 606, while
every other concept reports 2025-09-27.

**Sources keep their own taxonomy and currency.** SEC files `us-gaap` in USD;
DART accounts are normalised onto the same concept names but keep the `dart`
taxonomy and report in KRW. Both are queryable as one vocabulary of concepts
without ever merging into one series of facts, which would compare a dollar
against a won.

`ingested_before` and `source` are threaded through every call. They are the
two axes a backtest needs to pin, and a helper that quietly dropped one would
be the easiest place in the system to reintroduce a leak — so they live on a
resolver that every lookup goes through, rather than on each call site.
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

# Which taxonomy each source files under.
TAXONOMY_FOR: dict[FundamentalSource, str] = {
    FundamentalSource.SEC: "us-gaap",
    FundamentalSource.DART: "dart",
    FundamentalSource.YFINANCE: "yfinance",
}

# Concepts quoted per share rather than as an amount. Units matter: SEC nests
# facts by unit precisely so a USD revenue and a USD/shares EPS cannot collide.
PER_SHARE = frozenset({"EarningsPerShareBasic", "EarningsPerShareDiluted"})

# Preference order for choosing the anchor. Net income is tagged by essentially
# every filer in every period, which makes it the most reliable spine; the rest
# are fallbacks for filers that do not.
ANCHOR_CANDIDATES = (
    "NetIncomeLoss",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "EarningsPerShareBasic",
)


def unit_for(concept: str, currency: str) -> str:
    return f"{currency}/shares" if concept in PER_SHARE else currency


def _absent(concept: str, reason: str) -> ReportedValue:
    return ReportedValue(
        concept=concept, value=None, outcome="NO_OBSERVATION_IN_SOURCE", explanation=reason
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


class _Resolver:
    """Carries the axes every lookup must respect, so no call site can omit one."""

    def __init__(
        self,
        session: Session,
        instrument_id: int,
        *,
        asof: datetime,
        currency: str,
        policy: RevisionPolicy,
        ingested_before: datetime | None,
        source: FundamentalSource | None,
    ) -> None:
        self.session = session
        self.instrument_id = instrument_id
        self.asof = asof
        self.currency = currency
        self.policy = policy
        self.ingested_before = ingested_before
        self.source = source
        self.taxonomy = TAXONOMY_FOR[source] if source else "us-gaap"

    def latest(self, concept: str) -> ReportedValue:
        """Whatever period is most recent for this concept."""
        result = fundamental_repo.latest_value_as_of(
            self.session,
            self.instrument_id,
            concept=concept,
            unit=unit_for(concept, self.currency),
            taxonomy=self.taxonomy,
            asof=self.asof,
            months=REQUIRED_MONTHS[concept],
            policy=self.policy,
            ingested_before=self.ingested_before,
            source=self.source,
        )
        return _from_lookup(concept, result)

    def at_period(
        self, concept: str, *, period_start: date | None, period_end: date
    ) -> ReportedValue:
        """This concept at exactly one period, or a reasoned absence."""
        result = fundamental_repo.value_as_of(
            self.session,
            self.instrument_id,
            FundamentalContext(
                taxonomy=self.taxonomy,
                concept=concept,
                unit=unit_for(concept, self.currency),
                period_end=period_end,
                period_start=period_start,
            ),
            asof=self.asof,
            policy=self.policy,
            ingested_before=self.ingested_before,
            source=self.source,
        )
        if result.fact is None:
            return _absent(
                concept,
                f"not tagged for the fiscal period ending {period_end}; using "
                "another period would produce a ratio describing no real period",
            )
        return _from_lookup(concept, result)

    def previous_annual(self, concept: str, *, before_period_end: date) -> ReportedValue:
        """The prior fiscal year, chosen by calendar and read at the current asof.

        Stepping back one fiscal period rather than 365 days matters twice
        over. The period must be the one immediately before the anchor —
        Apple's FY2024 became usable 2024-11-04, so a score run on 2025-11-03
        put `asof - 365` one day short and fell through to FY2023, reporting a
        728-day change as year-on-year growth. And the revision must be the one
        knowable *now*, since asking as of a year ago hides every restatement
        published since.
        """
        result = fundamental_repo.previous_annual_fact(
            self.session,
            self.instrument_id,
            concept=concept,
            unit=unit_for(concept, self.currency),
            taxonomy=self.taxonomy,
            before_period_end=before_period_end,
            asof=self.asof,
            policy=self.policy,
            ingested_before=self.ingested_before,
            source=self.source,
        )
        return _from_lookup(concept, result)

    def anchor(self) -> ReportedValue | None:
        """The fiscal period every input will be pinned to."""
        for concept in ANCHOR_CANDIDATES:
            candidate = self.latest(concept)
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

    The prior-year block steps back a fiscal period rather than a calendar
    year, for the reasons on `_Resolver.previous_annual`.
    """
    resolver = _Resolver(
        session,
        instrument_id,
        asof=asof,
        currency=currency,
        policy=policy,
        ingested_before=ingested_before,
        source=source,
    )

    anchor = resolver.anchor()

    if anchor is None or anchor.period_end is None:
        # Nothing to anchor to. Report each concept's own absence, which
        # carries the repository's reason for why it is missing.
        return FundamentalSnapshot(
            instrument_id=instrument_id,
            asof=asof,
            price=price,
            currency=currency,
            values={concept: resolver.latest(concept) for concept in REQUIRED_MONTHS},
            anchor_period_end=None,
        )

    # Duration facts share the anchor's span; instantaneous ones are measured
    # at its end date and carry no start at all.
    values = {
        concept: resolver.at_period(
            concept,
            period_start=anchor.period_start if months is not None else None,
            period_end=anchor.period_end,
        )
        for concept, months in REQUIRED_MONTHS.items()
    }

    prior = {
        concept: resolver.previous_annual(concept, before_period_end=anchor.period_end)
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
