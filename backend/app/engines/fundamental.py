"""Fundamental factor engine.

Scores a company's reported financials. Like the technical engine it is pure —
it receives already-resolved values and returns a `Factor`, and the
import-linter contract forbids it from reaching the database. The
point-in-time work has already happened by the time a value arrives here; the
engine's job is to turn numbers into a comparable score without quietly
inventing any.

**Absence is carried, not flattened.** Each input arrives as a `ReportedValue`
that knows why it is empty — outside source coverage, absent from the source,
or genuinely unfiled. The engine propagates that reason into the factor's
availability message rather than substituting a zero or a sector average.
Filling a gap with a plausible number is how a backtest starts quietly
outperforming reality.

**Annual figures only, for now.** A quarterly EPS in a P/E would understate the
ratio roughly fourfold, and mixing a quarterly numerator with an annual
denominator is the same error in a different place. The caller is responsible
for requesting twelve-month periods; `REQUIRED_MONTHS` documents which concepts
are durations and which are instantaneous.

**The monotonic ratios are ranked, not mapped.** ROE, debt ratio, operating
margin and revenue growth are scored as a percentile within the same market's
universe at the same instant, because "good" for each of them is a statement
about peers rather than an absolute. Valuation is not, and `CROSS_SECTIONAL`
says why P/E keeps its fixed scale. A caller that passes no peer group gets the
fixed scale throughout, and every metric names the scale that produced it, so
the two can never be read as the same measurement.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.core.normalize import bounded, clamp_score, peak_at, percentile_rank
from app.core.types import (
    Availability,
    DataProvenance,
    Engine,
    Factor,
    Metric,
    ReasonStatus,
    SignalReason,
)

# Duration in months each concept covers. None marks an instantaneous fact —
# a balance measured at a date rather than across a span.
REQUIRED_MONTHS: Mapping[str, int | None] = {
    "Revenues": 12,
    "RevenueFromContractWithCustomerExcludingAssessedTax": 12,
    "NetIncomeLoss": 12,
    "OperatingIncomeLoss": 12,
    "EarningsPerShareBasic": 12,
    "EarningsPerShareDiluted": 12,
    "Assets": None,
    "Liabilities": None,
    "StockholdersEquity": None,
    "CashAndCashEquivalentsAtCarryingValue": None,
}


@dataclass(frozen=True, slots=True)
class ReportedValue:
    """One resolved financial figure, or a reasoned absence.

    `explanation` comes from the repository's `FactLookup`, so the wording a
    user reads about a missing input is the same wording the lookup produced.
    """

    concept: str
    value: Decimal | None
    outcome: str
    explanation: str
    filed_at: date | None = None
    form: str | None = None
    period_end: date | None = None
    period_start: date | None = None

    @property
    def present(self) -> bool:
        return self.value is not None

    def as_float(self) -> float | None:
        return float(self.value) if self.value is not None else None


@dataclass(frozen=True, slots=True)
class FundamentalSnapshot:
    """Everything the engine needs, already resolved to a point in time."""

    instrument_id: int
    asof: datetime
    price: float | None
    currency: str
    values: Mapping[str, ReportedValue]
    prior_year: Mapping[str, ReportedValue] | None = None
    anchor_period_end: date | None = None

    def get(self, concept: str) -> ReportedValue | None:
        return self.values.get(concept)

    def number(self, concept: str) -> float | None:
        entry = self.values.get(concept)
        return entry.as_float() if entry else None

    def aligned(self, *concepts: str) -> tuple[float, ...] | None:
        """Values for `concepts`, only if they all describe one period.

        Returns None when any is missing or when their `period_end` dates
        disagree. A ratio across two periods is not an approximation — it
        describes no period that existed, and it looks entirely normal, so
        the only safe response is to decline to compute it.

        The assembler already pins everything to one anchor, making this a
        second line of defence. It is worth having: this is the last point
        before a number reaches a user, and a future caller assembling a
        snapshot by hand would otherwise reintroduce the fault silently.
        """
        entries = [self.values.get(c) for c in concepts]
        if any(e is None or not e.present for e in entries):
            return None

        periods = {e.period_end for e in entries if e is not None}
        if len(periods) > 1:
            return None

        return tuple(e.as_float() for e in entries if e is not None)  # type: ignore[misc]

    def revenue_entry(self) -> ReportedValue | None:
        """Revenue under either of the two tags filers use.

        `Revenues` is the older tag; ASC 606 filers commonly use
        `RevenueFromContractWithCustomerExcludingAssessedTax` instead. Treating
        only one as canonical loses whole companies, and preferring the wrong
        one is silent — Apple's `Revenues` series stops in 2018, so a margin
        built on it would divide current income by seven-year-old revenue.
        """
        for concept in (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
        ):
            entry = self.values.get(concept)
            if entry is not None and entry.present:
                return entry
        return None

    def revenue(self) -> float | None:
        entry = self.revenue_entry()
        return entry.as_float() if entry else None

    def prior_revenue(self) -> float | None:
        if not self.prior_year:
            return None
        for concept in (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
        ):
            entry = self.prior_year.get(concept)
            if entry and entry.value is not None:
                return float(entry.value)
        return None


@dataclass(frozen=True, slots=True)
class FundamentalParams:
    """Where the opinions live.

    Every bound here is a judgement about what counts as good, so it belongs to
    the strategy version rather than being a constant. Moving `per_ideal` from
    15 to 25 produces different signals from identical filings.
    """

    # P/E is scored as a distance from a reasonable multiple, not monotonically.
    # A very low P/E is as often distress as value, and a negative one means
    # the company lost money — which a "lower is better" rule would reward.
    per_ideal: float = 15.0
    per_tolerance: float = 25.0

    roe_low: float = 0.0
    roe_high: float = 0.30

    debt_ratio_low: float = 0.2
    debt_ratio_high: float = 0.8

    operating_margin_low: float = 0.0
    operating_margin_high: float = 0.30

    revenue_growth_low: float = -0.20
    revenue_growth_high: float = 0.40

    # How much of the picture must be visible before a score is worth stating.
    # Without a floor, one metric out of five carries the whole factor: a lone
    # ROE of 100 would produce a fundamental score of 100 at full weight, which
    # reads as a strong company when it actually means "we could compute one
    # thing". That contradicts the rule the rest of the system follows, that
    # absence is never quietly turned into a judgement.
    min_metrics: int = 3
    require_profitability: bool = True

    # How many peers a rank needs before it says anything. A percentile over
    # three values can only return 17, 50 or 83, which reads as a considered
    # position and is really the arithmetic of having almost nothing to
    # compare against. Below this the metric falls back to its fixed scale and
    # the metric's own `detail` says so, so the fallback is visible in the
    # stored row rather than only in the code that ran.
    min_peers: int = 5


# Metrics that say whether the business earns anything. A score built only
# from leverage and growth describes a company nobody has checked is profitable.
PROFITABILITY_METRICS = frozenset({"ROE", "Operating margin"})


# Which ratios are ranked against peers, and which direction is good.
#
# P/E is deliberately absent. It is scored as a distance from a reasonable
# multiple, and a rank over raw multiples can only say "cheapest is best" or
# "dearest is best" — the first is the rule this engine already declines to
# follow, because a very low multiple is as often distress as value. Ranking
# the distance instead would keep the belief but lose what the fixed scale
# knows: that a P/E of 8 is near a reasonable multiple even in a market where
# every peer trades at 8. So valuation keeps its absolute opinion and the four
# monotonic ratios below become relative ones.
CROSS_SECTIONAL: Mapping[str, bool] = {
    "ROE": True,
    "Debt ratio": False,
    "Operating margin": True,
    "Revenue growth": True,
}


# --- raw comparable values -------------------------------------------------
#
# One source of truth for both sides of a comparison. A population assembled
# under different eligibility rules from the value being ranked would produce
# a rank that describes nothing, and nothing about it would look wrong — so
# the engine and `comparable_ratios` call these same functions.
#
# Each returns None when the snapshot cannot contribute that ratio at all,
# which covers both a missing input and an uninterpretable one. Negative
# equity is the case worth naming: the engine scores it as an explicit zero,
# but the number itself must stay out of every peer's denominator, because a
# loss divided by negative equity comes out positive and would drag the whole
# market's ranks toward it.


def _roe_of(snapshot: FundamentalSnapshot) -> float | None:
    pair = snapshot.aligned("NetIncomeLoss", "StockholdersEquity")
    if pair is None:
        return None
    income, equity = pair
    if equity <= 0:
        return None
    return income / equity


def _debt_ratio_of(snapshot: FundamentalSnapshot) -> float | None:
    pair = snapshot.aligned("Liabilities", "Assets")
    if pair is None:
        return None
    liabilities, assets = pair
    if assets <= 0:
        return None
    return liabilities / assets


def _operating_margin_of(snapshot: FundamentalSnapshot) -> float | None:
    operating_entry = snapshot.values.get("OperatingIncomeLoss")
    revenue_entry = snapshot.revenue_entry()
    if (
        operating_entry is None
        or not operating_entry.present
        or revenue_entry is None
        or operating_entry.period_end != revenue_entry.period_end
    ):
        # The exact case Apple produces: operating income at FY2025 against
        # a `Revenues` tag frozen at FY2018.
        return None

    operating = operating_entry.as_float()
    revenue = revenue_entry.as_float()
    if operating is None or revenue is None or revenue <= 0:
        return None
    return operating / revenue


def _revenue_growth_of(snapshot: FundamentalSnapshot) -> float | None:
    current = snapshot.revenue()
    prior = snapshot.prior_revenue()
    if current is None or prior is None or prior <= 0:
        return None
    return (current - prior) / prior


_RATIO_OF: Mapping[str, Callable[[FundamentalSnapshot], float | None]] = {
    "ROE": _roe_of,
    "Debt ratio": _debt_ratio_of,
    "Operating margin": _operating_margin_of,
    "Revenue growth": _revenue_growth_of,
}


def comparable_ratios(snapshot: FundamentalSnapshot) -> dict[str, float]:
    """What this snapshot contributes to its market's peer population.

    Only the ratios in `CROSS_SECTIONAL`, and only where the snapshot can
    state them. Values are the plain ratios rather than the percentages the
    metrics carry; a rank is unchanged by that, and keeping one convention
    means a population and a score can never be built in different units.
    """
    out: dict[str, float] = {}
    for name, fn in _RATIO_OF.items():
        value = fn(snapshot)
        if value is not None:
            out[name] = value
    return out


@dataclass(frozen=True, slots=True)
class PeerRatios:
    """The same ratios across one market's universe at one instant.

    Market-scoped rather than universe-wide: currency, accounting standard and
    filing source already split on market, and a P/E distribution in Seoul is
    not the one in New York. Scoped by sector would be closer to what a rank
    is supposed to mean, but this watchlist holds one or two names in most
    sectors, and a percentile over one peer returns 50 for everybody.

    The instrument being scored is part of its own population. Excluding it
    would make the best performer in a nine-name market score 100 on the
    strength of eight comparisons, and the midpoint convention already keeps a
    self-comparison from being worth a full rank.
    """

    asof: datetime
    values: Mapping[str, tuple[float, ...]]

    def population(self, metric: str) -> tuple[float, ...]:
        return self.values.get(metric, ())

    @classmethod
    def of(cls, asof: datetime, snapshots: Iterable[FundamentalSnapshot]) -> PeerRatios:
        """Assemble a population from every snapshot that can contribute."""
        gathered: dict[str, list[float]] = {name: [] for name in CROSS_SECTIONAL}
        for snapshot in snapshots:
            for name, value in comparable_ratios(snapshot).items():
                gathered[name].append(value)
        return cls(asof=asof, values={k: tuple(v) for k, v in gathered.items()})


class FundamentalEngine:
    """Scores reported financials. Implements the `FactorEngine` protocol."""

    engine = Engine.FUNDAMENTAL

    def __init__(self, params: FundamentalParams | None = None) -> None:
        self.params = params or FundamentalParams()

    def evaluate(
        self,
        snapshot: FundamentalSnapshot,
        *,
        requested_weight: float,
        provenance: DataProvenance,
        peers: PeerRatios | None = None,
    ) -> tuple[Factor, tuple[SignalReason, ...]]:
        """Compute the factor and the evidence lines that explain it.

        `peers` is the same-instant population this instrument is ranked
        within. Passing None scores every ratio on its fixed scale, which is
        the honest answer when there is no universe to compare against — and
        each metric says which scale produced it, so the two cannot be
        confused after the fact.
        """
        metrics: list[Metric] = []
        reasons: list[SignalReason] = []

        self._price_to_earnings(snapshot, metrics, reasons, peers)
        self._return_on_equity(snapshot, metrics, reasons, peers)
        self._debt_ratio(snapshot, metrics, reasons, peers)
        self._operating_margin(snapshot, metrics, reasons, peers)
        self._revenue_growth(snapshot, metrics, reasons, peers)

        if not metrics:
            # Nothing computed at all. `_unavailable` explains each absent
            # input rather than stating a coverage shortfall, which would be
            # a less useful answer than naming what is actually missing.
            return self._unavailable(snapshot, requested_weight, provenance), ()

        shortfall = self._coverage_shortfall(metrics)
        if shortfall is not None:
            return (
                self._unavailable(snapshot, requested_weight, provenance, override=shortfall),
                (),
            )

        score = sum(m.normalized for m in metrics) / len(metrics)

        factor = Factor(
            engine=Engine.FUNDAMENTAL,
            score=clamp_score(score),
            metrics=tuple(metrics),
            requested_weight=requested_weight,
            effective_weight=requested_weight,
            availability=Availability.AVAILABLE,
            provenance=provenance,
        )
        return factor, tuple(reasons)

    def _coverage_shortfall(self, metrics: list[Metric]) -> str | None:
        """Why this factor should sit out, or None if it may be scored.

        Callers must handle the empty case before reaching here; an empty set
        has no shortfall to describe, only absent inputs to explain.
        """
        names = {m.name for m in metrics}

        if len(metrics) < self.params.min_metrics:
            return (
                f"only {len(metrics)} of the ratios could be computed "
                f"({', '.join(sorted(names))}); at least {self.params.min_metrics} "
                "are needed before a score means anything"
            )

        if self.params.require_profitability and not (names & PROFITABILITY_METRICS):
            return (
                "no profitability measure available "
                f"({' or '.join(sorted(PROFITABILITY_METRICS))}); leverage and "
                "growth alone do not say whether the business earns"
            )

        return None

    # --- normalization ----------------------------------------------------

    def _position(
        self,
        name: str,
        raw: float,
        peers: PeerRatios | None,
        *,
        fixed: Callable[[float], float],
    ) -> tuple[float, str]:
        """Put `raw` on 0-100, and say which scale did it.

        The phrase comes back with the score and is appended to the metric's
        `detail`, so a stored row states whether it was ranked or mapped. A
        percentile that silently became a fixed scale because the population
        was thin would be a different measurement wearing the same name.
        """
        higher_is_better = CROSS_SECTIONAL.get(name)
        if higher_is_better is None:
            return fixed(raw), "fixed scale"
        if peers is None:
            return fixed(raw), "fixed scale, no peer group"

        population = peers.population(name)
        if len(population) < self.params.min_peers:
            return fixed(raw), f"fixed scale, only {len(population)} in the peer group"

        rank = percentile_rank(raw, population)
        if not higher_is_better:
            rank = 100.0 - rank
        return rank, f"ranked against {len(population)} peers"

    # --- individual ratios ------------------------------------------------

    def _price_to_earnings(
        self,
        snapshot: FundamentalSnapshot,
        metrics: list[Metric],
        reasons: list[SignalReason],
        peers: PeerRatios | None,
    ) -> None:
        eps = snapshot.number("EarningsPerShareBasic")
        if snapshot.price is None or eps is None:
            return

        if eps <= 0:
            # A negative or zero P/E is not a cheap stock, it is a company that
            # did not earn. Scoring the ratio would produce a flattering number.
            metrics.append(Metric("P/E", raw=0.0, normalized=0.0, detail="loss-making"))
            reasons.append(
                SignalReason(
                    ReasonStatus.OPPOSES,
                    f"Negative or zero annual EPS ({eps:.2f}) — no meaningful P/E",
                    Engine.FUNDAMENTAL,
                    "P/E",
                )
            )
            return

        per = snapshot.price / eps
        # Routed through `_position` like every other ratio even though
        # `CROSS_SECTIONAL` will send it straight back. Scoring it inline
        # would leave one metric that does not name its scale, and would let a
        # later decision to rank valuation pass silently through a call site
        # that never asked about peers.
        score, scale = self._position(
            "P/E",
            per,
            peers,
            fixed=lambda v: peak_at(v, self.params.per_ideal, self.params.per_tolerance),
        )
        metrics.append(
            Metric(
                "P/E",
                raw=per,
                normalized=score,
                detail=f"price / annual basic EPS · {scale}",
            )
        )
        reasons.append(self._per_reason(per))

    def _return_on_equity(
        self,
        snapshot: FundamentalSnapshot,
        metrics: list[Metric],
        reasons: list[SignalReason],
        peers: PeerRatios | None,
    ) -> None:
        pair = snapshot.aligned("NetIncomeLoss", "StockholdersEquity")
        if pair is None:
            return
        _, equity = pair
        if equity == 0:
            return

        if equity < 0:
            # Negative equity makes the ratio meaningless rather than excellent:
            # a loss divided by negative equity comes out positive. Scored as an
            # explicit zero here, and `_roe_of` keeps the number itself out of
            # every peer's population for the same reason.
            metrics.append(Metric("ROE", raw=0.0, normalized=0.0, detail="negative equity"))
            reasons.append(
                SignalReason(
                    ReasonStatus.OPPOSES,
                    "Shareholders' equity is negative — ROE is not interpretable",
                    Engine.FUNDAMENTAL,
                    "ROE",
                )
            )
            return

        roe = _roe_of(snapshot)
        if roe is None:
            return

        score, scale = self._position(
            "ROE",
            roe,
            peers,
            fixed=lambda v: bounded(v, self.params.roe_low, self.params.roe_high),
        )
        metrics.append(
            Metric(
                "ROE",
                raw=roe * 100.0,
                normalized=score,
                detail=f"net income / equity, % · {scale}",
            )
        )
        reasons.append(self._roe_reason(roe))

    def _debt_ratio(
        self,
        snapshot: FundamentalSnapshot,
        metrics: list[Metric],
        reasons: list[SignalReason],
        peers: PeerRatios | None,
    ) -> None:
        ratio = _debt_ratio_of(snapshot)
        if ratio is None:
            return

        score, scale = self._position(
            "Debt ratio",
            ratio,
            peers,
            fixed=lambda v: bounded(
                v, self.params.debt_ratio_low, self.params.debt_ratio_high, invert=True
            ),
        )
        metrics.append(
            Metric(
                "Debt ratio",
                raw=ratio * 100.0,
                normalized=score,
                detail=f"liabilities / assets, % · {scale}",
            )
        )
        reasons.append(self._debt_reason(ratio))

    def _operating_margin(
        self,
        snapshot: FundamentalSnapshot,
        metrics: list[Metric],
        reasons: list[SignalReason],
        peers: PeerRatios | None,
    ) -> None:
        margin = _operating_margin_of(snapshot)
        if margin is None:
            return

        score, scale = self._position(
            "Operating margin",
            margin,
            peers,
            fixed=lambda v: bounded(
                v, self.params.operating_margin_low, self.params.operating_margin_high
            ),
        )
        metrics.append(
            Metric(
                "Operating margin",
                raw=margin * 100.0,
                normalized=score,
                detail=f"operating income / revenue, % · {scale}",
            )
        )
        reasons.append(self._margin_reason(margin))

    def _revenue_growth(
        self,
        snapshot: FundamentalSnapshot,
        metrics: list[Metric],
        reasons: list[SignalReason],
        peers: PeerRatios | None,
    ) -> None:
        growth = _revenue_growth_of(snapshot)
        if growth is None:
            return

        score, scale = self._position(
            "Revenue growth",
            growth,
            peers,
            fixed=lambda v: bounded(
                v, self.params.revenue_growth_low, self.params.revenue_growth_high
            ),
        )
        metrics.append(
            Metric(
                "Revenue growth",
                raw=growth * 100.0,
                normalized=score,
                detail=f"year over year, % · {scale}",
            )
        )
        reasons.append(self._growth_reason(growth))

    # --- evidence ---------------------------------------------------------

    def _per_reason(self, per: float) -> SignalReason:
        p = self.params
        if per > p.per_ideal + p.per_tolerance / 2:
            return SignalReason(
                ReasonStatus.OPPOSES, f"P/E {per:.1f} — richly valued", Engine.FUNDAMENTAL, "P/E"
            )
        if per < p.per_ideal - p.per_tolerance / 2:
            return SignalReason(
                ReasonStatus.NEUTRAL,
                f"P/E {per:.1f} — cheap, though a very low multiple can signal distress",
                Engine.FUNDAMENTAL,
                "P/E",
            )
        return SignalReason(
            ReasonStatus.SUPPORTS, f"P/E {per:.1f} — unremarkable", Engine.FUNDAMENTAL, "P/E"
        )

    @staticmethod
    def _roe_reason(roe: float) -> SignalReason:
        if roe >= 0.15:
            return SignalReason(
                ReasonStatus.SUPPORTS, f"ROE {roe * 100:.1f}%", Engine.FUNDAMENTAL, "ROE"
            )
        if roe <= 0:
            return SignalReason(
                ReasonStatus.OPPOSES,
                f"ROE {roe * 100:.1f}% — losing money",
                Engine.FUNDAMENTAL,
                "ROE",
            )
        return SignalReason(
            ReasonStatus.NEUTRAL, f"ROE {roe * 100:.1f}% — modest", Engine.FUNDAMENTAL, "ROE"
        )

    @staticmethod
    def _debt_reason(ratio: float) -> SignalReason:
        if ratio >= 0.7:
            return SignalReason(
                ReasonStatus.OPPOSES,
                f"Liabilities are {ratio * 100:.0f}% of assets",
                Engine.FUNDAMENTAL,
                "Debt ratio",
            )
        return SignalReason(
            ReasonStatus.SUPPORTS,
            f"Liabilities are {ratio * 100:.0f}% of assets",
            Engine.FUNDAMENTAL,
            "Debt ratio",
        )

    @staticmethod
    def _margin_reason(margin: float) -> SignalReason:
        if margin <= 0:
            return SignalReason(
                ReasonStatus.OPPOSES,
                f"Operating margin {margin * 100:.1f}% — operating at a loss",
                Engine.FUNDAMENTAL,
                "Operating margin",
            )
        if margin >= 0.20:
            return SignalReason(
                ReasonStatus.SUPPORTS,
                f"Operating margin {margin * 100:.1f}%",
                Engine.FUNDAMENTAL,
                "Operating margin",
            )
        return SignalReason(
            ReasonStatus.NEUTRAL,
            f"Operating margin {margin * 100:.1f}%",
            Engine.FUNDAMENTAL,
            "Operating margin",
        )

    @staticmethod
    def _growth_reason(growth: float) -> SignalReason:
        if growth >= 0.10:
            return SignalReason(
                ReasonStatus.SUPPORTS,
                f"Revenue {growth * 100:+.1f}% year over year",
                Engine.FUNDAMENTAL,
                "Revenue growth",
            )
        if growth < 0:
            return SignalReason(
                ReasonStatus.OPPOSES,
                f"Revenue {growth * 100:+.1f}% year over year",
                Engine.FUNDAMENTAL,
                "Revenue growth",
            )
        return SignalReason(
            ReasonStatus.NEUTRAL,
            f"Revenue {growth * 100:+.1f}% year over year",
            Engine.FUNDAMENTAL,
            "Revenue growth",
        )

    # --- absence ----------------------------------------------------------

    @staticmethod
    def _unavailable(
        snapshot: FundamentalSnapshot,
        requested_weight: float,
        provenance: DataProvenance,
        *,
        override: str | None = None,
    ) -> Factor:
        """No ratio could be computed — say precisely why.

        The reasons come from the repository's own lookups, so a user is told
        whether the figure was never filed, is absent from our source, or falls
        outside what the source covers. Those are different situations and
        collapsing them into "no data" would throw away the distinction the
        point-in-time work exists to preserve.
        """
        if override is not None:
            return Factor(
                engine=Engine.FUNDAMENTAL,
                score=0.0,
                metrics=(),
                requested_weight=requested_weight,
                effective_weight=0.0,
                availability=Availability.UNAVAILABLE,
                provenance=provenance,
                availability_reason=override,
            )

        absent = [v for v in snapshot.values.values() if not v.present]
        if absent:
            # One representative explanation, plus a count. Listing ten
            # near-identical sentences helps nobody.
            lead = absent[0]
            extra = f" (and {len(absent) - 1} more)" if len(absent) > 1 else ""
            reason = f"{lead.concept}: {lead.explanation}{extra}"
        elif snapshot.price is None:
            reason = "no price available to compute valuation ratios"
        else:
            reason = "no fundamental inputs available"

        return Factor(
            engine=Engine.FUNDAMENTAL,
            score=0.0,
            metrics=(),
            requested_weight=requested_weight,
            effective_weight=0.0,
            availability=Availability.UNAVAILABLE,
            provenance=provenance,
            availability_reason=reason,
        )
