"""Period alignment and metric coverage.

Two rules that both exist because the failure mode is a plausible-looking
number rather than an error.

**A ratio must describe one period.** Mixing FY2025 operating income with a
FY2018 revenue tag yields a percentage that looks entirely normal and describes
no period that ever existed. Apple's filings produce exactly this pairing: its
`Revenues` series stops in 2018 after it adopted ASC 606, while everything else
runs to the present. The assembler pins all inputs to one anchor period, and
the engine refuses any ratio whose inputs disagree — two independent guards,
because this is the last point before a number reaches a user.

**A score needs enough of the picture.** With no floor, one metric carries the
whole factor: a lone ROE of 100 produces a fundamental score of 100 at full
weight, which reads as a strong company when it means "we could compute one
thing". That is the same mistake as scoring missing data as zero, wearing
better clothes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.core.types import Availability, DataProvenance, Freshness
from app.engines.fundamental import (
    FundamentalEngine,
    FundamentalParams,
    FundamentalSnapshot,
    ReportedValue,
)

ASOF = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
FY2025 = date(2025, 9, 27)
FY2018 = date(2018, 9, 29)

FRESH = DataProvenance(
    source_asof=ASOF, source_checked_at=ASOF, data_age=timedelta(0), freshness=Freshness.FRESH
)


def value(concept: str, amount: str, period_end: date) -> ReportedValue:
    return ReportedValue(
        concept=concept,
        value=Decimal(amount),
        outcome="FOUND",
        explanation="present",
        filed_at=date(2025, 10, 31),
        form="10-K",
        period_end=period_end,
    )


def absent(concept: str) -> ReportedValue:
    return ReportedValue(
        concept=concept,
        value=None,
        outcome="NO_OBSERVATION_IN_SOURCE",
        explanation=f"{concept} is not tagged for this period",
    )


def build(values: dict[str, ReportedValue], *, price: float | None = 336.13) -> FundamentalSnapshot:
    return FundamentalSnapshot(
        instrument_id=1,
        asof=ASOF,
        price=price,
        currency="USD",
        values=values,
        anchor_period_end=FY2025,
    )


# Coverage is switched off in the alignment tests so that they observe
# alignment alone. With it on, the two guards compose and an excluded ratio
# can trip the coverage gate, emptying the metric list and hiding which rule
# actually fired.
NO_COVERAGE_GATE = FundamentalParams(min_metrics=1, require_profitability=False)


def run(snap: FundamentalSnapshot, params: FundamentalParams | None = None):  # type: ignore[no-untyped-def]
    return FundamentalEngine(params or NO_COVERAGE_GATE).evaluate(
        snap, requested_weight=0.4, provenance=FRESH
    )


def names(factor: object) -> set[str]:
    return {m.name for m in factor.metrics}  # type: ignore[attr-defined]


class TestPeriodAlignment:
    def test_operating_margin_refuses_a_stale_revenue_tag(self) -> None:
        """The exact pairing Apple's filings offer up.

        FY2025 operating income over FY2018 revenue would read as roughly 50%,
        a plausible figure describing nothing.
        """
        snap = build(
            {
                "OperatingIncomeLoss": value("OperatingIncomeLoss", "133050000000", FY2025),
                "Revenues": value("Revenues", "265595000000", FY2018),
                "RevenueFromContractWithCustomerExcludingAssessedTax": absent(
                    "RevenueFromContractWithCustomerExcludingAssessedTax"
                ),
                "NetIncomeLoss": value("NetIncomeLoss", "112010000000", FY2025),
                "StockholdersEquity": value("StockholdersEquity", "73733000000", FY2025),
            }
        )

        factor, _ = run(snap)

        assert "Operating margin" not in names(factor)
        assert "ROE" in names(factor), "the aligned ratio must still compute"

    def test_it_computes_when_the_periods_match(self) -> None:
        snap = build(
            {
                "OperatingIncomeLoss": value("OperatingIncomeLoss", "133050000000", FY2025),
                "RevenueFromContractWithCustomerExcludingAssessedTax": value(
                    "RevenueFromContractWithCustomerExcludingAssessedTax", "416161000000", FY2025
                ),
                "Revenues": absent("Revenues"),
                "NetIncomeLoss": value("NetIncomeLoss", "112010000000", FY2025),
                "StockholdersEquity": value("StockholdersEquity", "73733000000", FY2025),
            }
        )

        factor, _ = run(snap)

        assert "Operating margin" in names(factor)

    def test_roe_refuses_a_mismatched_balance(self) -> None:
        """Annual income over a balance from another year is not an ROE."""
        snap = build(
            {
                "NetIncomeLoss": value("NetIncomeLoss", "112010000000", FY2025),
                "StockholdersEquity": value("StockholdersEquity", "107520000000", FY2018),
                "Assets": value("Assets", "359241000000", FY2025),
                "Liabilities": value("Liabilities", "285508000000", FY2025),
            }
        )

        factor, _ = run(snap)

        assert "ROE" not in names(factor)
        assert "Debt ratio" in names(factor)

    def test_debt_ratio_refuses_a_mismatched_balance(self) -> None:
        snap = build(
            {
                "Assets": value("Assets", "359241000000", FY2025),
                "Liabilities": value("Liabilities", "258578000000", FY2018),
                "NetIncomeLoss": value("NetIncomeLoss", "112010000000", FY2025),
                "StockholdersEquity": value("StockholdersEquity", "73733000000", FY2025),
            }
        )

        factor, _ = run(snap)

        assert "Debt ratio" not in names(factor)
        assert "ROE" in names(factor)

    def test_aligned_returns_none_on_a_missing_input(self) -> None:
        snap = build(
            {
                "NetIncomeLoss": value("NetIncomeLoss", "1", FY2025),
                "StockholdersEquity": absent("StockholdersEquity"),
            }
        )

        assert snap.aligned("NetIncomeLoss", "StockholdersEquity") is None


class TestCoveragePolicy:
    def test_one_metric_does_not_carry_the_whole_factor(self) -> None:
        """A lone ROE of 100 must not read as a strong company."""
        snap = build(
            {
                "NetIncomeLoss": value("NetIncomeLoss", "112010000000", FY2025),
                "StockholdersEquity": value("StockholdersEquity", "73733000000", FY2025),
            },
            price=None,
        )

        factor, _ = FundamentalEngine().evaluate(snap, requested_weight=0.4, provenance=FRESH)

        assert factor.availability is Availability.UNAVAILABLE
        assert factor.effective_weight == 0.0
        assert factor.availability_reason is not None
        assert "3개 이상" in factor.availability_reason

    def test_enough_metrics_scores_normally(self) -> None:
        snap = build(
            {
                "NetIncomeLoss": value("NetIncomeLoss", "112010000000", FY2025),
                "StockholdersEquity": value("StockholdersEquity", "73733000000", FY2025),
                "Assets": value("Assets", "359241000000", FY2025),
                "Liabilities": value("Liabilities", "285508000000", FY2025),
                "EarningsPerShareBasic": value("EarningsPerShareBasic", "7.49", FY2025),
            }
        )

        factor, _ = FundamentalEngine().evaluate(snap, requested_weight=0.4, provenance=FRESH)

        assert factor.availability is Availability.AVAILABLE
        assert factor.effective_weight == 0.4

    def test_leverage_and_valuation_alone_are_refused(self) -> None:
        """Nothing here says whether the business actually earns.

        Deliberately supplies three metrics — debt, valuation and growth — so
        the count rule passes and the profitability rule is the one on trial.
        """
        snap = FundamentalSnapshot(
            instrument_id=1,
            asof=ASOF,
            price=336.13,
            currency="USD",
            values={
                "Assets": value("Assets", "359241000000", FY2025),
                "Liabilities": value("Liabilities", "285508000000", FY2025),
                "EarningsPerShareBasic": value("EarningsPerShareBasic", "7.49", FY2025),
                "RevenueFromContractWithCustomerExcludingAssessedTax": value(
                    "RevenueFromContractWithCustomerExcludingAssessedTax", "416161000000", FY2025
                ),
            },
            prior_year={
                "RevenueFromContractWithCustomerExcludingAssessedTax": value(
                    "RevenueFromContractWithCustomerExcludingAssessedTax", "391035000000", FY2025
                ),
                "Revenues": absent("Revenues"),
            },
            anchor_period_end=FY2025,
        )

        factor, _ = FundamentalEngine().evaluate(
            snap,
            requested_weight=0.4,
            provenance=FRESH,
        )

        assert factor.availability is Availability.UNAVAILABLE
        assert factor.availability_reason is not None
        assert "수익성 지표" in factor.availability_reason

    def test_the_requirement_is_configurable(self) -> None:
        """It is a strategy opinion, not a law."""
        snap = build(
            {
                "Assets": value("Assets", "359241000000", FY2025),
                "Liabilities": value("Liabilities", "285508000000", FY2025),
                "EarningsPerShareBasic": value("EarningsPerShareBasic", "7.49", FY2025),
            }
        )

        factor, _ = FundamentalEngine(
            FundamentalParams(min_metrics=2, require_profitability=False)
        ).evaluate(snap, requested_weight=0.4, provenance=FRESH)

        assert factor.availability is Availability.AVAILABLE

    def test_the_reason_names_what_was_computed(self) -> None:
        """So a user can see which ratio survived rather than guessing."""
        snap = build(
            {
                "NetIncomeLoss": value("NetIncomeLoss", "112010000000", FY2025),
                "StockholdersEquity": value("StockholdersEquity", "73733000000", FY2025),
            },
            price=None,
        )

        factor, _ = FundamentalEngine().evaluate(snap, requested_weight=0.4, provenance=FRESH)

        assert factor.availability_reason is not None
        assert "ROE" in factor.availability_reason
