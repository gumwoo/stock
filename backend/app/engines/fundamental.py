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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.core.normalize import bounded, clamp_score, peak_at
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

    def get(self, concept: str) -> ReportedValue | None:
        return self.values.get(concept)

    def number(self, concept: str) -> float | None:
        entry = self.values.get(concept)
        return entry.as_float() if entry else None

    def revenue(self) -> float | None:
        """Revenue under either of the two tags filers use.

        `Revenues` is the older tag; ASC 606 filers commonly use
        `RevenueFromContractWithCustomerExcludingAssessedTax` instead. Treating
        only one as canonical loses whole companies.
        """
        for concept in (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
        ):
            value = self.number(concept)
            if value is not None:
                return value
        return None

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
    ) -> tuple[Factor, tuple[SignalReason, ...]]:
        """Compute the factor and the evidence lines that explain it."""
        metrics: list[Metric] = []
        reasons: list[SignalReason] = []

        self._price_to_earnings(snapshot, metrics, reasons)
        self._return_on_equity(snapshot, metrics, reasons)
        self._debt_ratio(snapshot, metrics, reasons)
        self._operating_margin(snapshot, metrics, reasons)
        self._revenue_growth(snapshot, metrics, reasons)

        if not metrics:
            return self._unavailable(snapshot, requested_weight, provenance), ()

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

    # --- individual ratios ------------------------------------------------

    def _price_to_earnings(
        self,
        snapshot: FundamentalSnapshot,
        metrics: list[Metric],
        reasons: list[SignalReason],
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
        score = peak_at(per, self.params.per_ideal, self.params.per_tolerance)
        metrics.append(Metric("P/E", raw=per, normalized=score, detail="price / annual basic EPS"))
        reasons.append(self._per_reason(per))

    def _return_on_equity(
        self,
        snapshot: FundamentalSnapshot,
        metrics: list[Metric],
        reasons: list[SignalReason],
    ) -> None:
        income = snapshot.number("NetIncomeLoss")
        equity = snapshot.number("StockholdersEquity")
        if income is None or equity is None or equity == 0:
            return

        if equity < 0:
            # Negative equity makes the ratio meaningless rather than excellent:
            # a loss divided by negative equity comes out positive.
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

        roe = income / equity
        score = bounded(roe, self.params.roe_low, self.params.roe_high)
        metrics.append(
            Metric("ROE", raw=roe * 100.0, normalized=score, detail="net income / equity, %")
        )
        reasons.append(self._roe_reason(roe))

    def _debt_ratio(
        self,
        snapshot: FundamentalSnapshot,
        metrics: list[Metric],
        reasons: list[SignalReason],
    ) -> None:
        liabilities = snapshot.number("Liabilities")
        assets = snapshot.number("Assets")
        if liabilities is None or assets is None or assets <= 0:
            return

        ratio = liabilities / assets
        score = bounded(ratio, self.params.debt_ratio_low, self.params.debt_ratio_high, invert=True)
        metrics.append(
            Metric(
                "Debt ratio",
                raw=ratio * 100.0,
                normalized=score,
                detail="liabilities / assets, %",
            )
        )
        reasons.append(self._debt_reason(ratio))

    def _operating_margin(
        self,
        snapshot: FundamentalSnapshot,
        metrics: list[Metric],
        reasons: list[SignalReason],
    ) -> None:
        operating = snapshot.number("OperatingIncomeLoss")
        revenue = snapshot.revenue()
        if operating is None or revenue is None or revenue <= 0:
            return

        margin = operating / revenue
        score = bounded(margin, self.params.operating_margin_low, self.params.operating_margin_high)
        metrics.append(
            Metric(
                "Operating margin",
                raw=margin * 100.0,
                normalized=score,
                detail="operating income / revenue, %",
            )
        )
        reasons.append(self._margin_reason(margin))

    def _revenue_growth(
        self,
        snapshot: FundamentalSnapshot,
        metrics: list[Metric],
        reasons: list[SignalReason],
    ) -> None:
        current = snapshot.revenue()
        prior = snapshot.prior_revenue()
        if current is None or prior is None or prior <= 0:
            return

        growth = (current - prior) / prior
        score = bounded(growth, self.params.revenue_growth_low, self.params.revenue_growth_high)
        metrics.append(
            Metric(
                "Revenue growth",
                raw=growth * 100.0,
                normalized=score,
                detail="year over year, %",
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
    ) -> Factor:
        """No ratio could be computed — say precisely why.

        The reasons come from the repository's own lookups, so a user is told
        whether the figure was never filed, is absent from our source, or falls
        outside what the source covers. Those are different situations and
        collapsing them into "no data" would throw away the distinction the
        point-in-time work exists to preserve.
        """
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
