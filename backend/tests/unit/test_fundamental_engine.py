"""Fundamental ratios, their guards, and how absence is carried.

Three of these pin problems found by running the engine against real Apple
filings rather than by reasoning about it:

* `Revenues` and `RevenueFromContractWithCustomerExcludingAssessedTax` are both
  live tags, and Apple's `Revenues` series stops in 2018 after it adopted
  ASC 606. Preferring the wrong one computes every margin against a
  seven-year-old denominator.
* A ratio mixing a flow and a stock has to take both from one period. Apple's
  ROE initially came out as FY2025 annual income over a balance nine months
  later — a figure corresponding to no real period.
* Negative equity and negative EPS both turn "lower is better" ratios into
  flattering nonsense, so they are handled explicitly rather than divided.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.types import Availability, DataProvenance, Engine, Freshness
from app.engines.fundamental import (
    FundamentalEngine,
    FundamentalParams,
    FundamentalSnapshot,
    ReportedValue,
)

ASOF = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
PERIOD = date(2025, 9, 27)

FRESH = DataProvenance(
    source_asof=ASOF,
    source_checked_at=ASOF,
    data_age=timedelta(0),
    freshness=Freshness.FRESH,
)


def value(concept: str, amount: str | None, *, outcome: str = "FOUND") -> ReportedValue:
    return ReportedValue(
        concept=concept,
        value=Decimal(amount) if amount is not None else None,
        outcome=outcome if amount is not None else "NO_OBSERVATION_IN_SOURCE",
        explanation="present" if amount is not None else "the source holds nothing here",
        filed_at=date(2025, 10, 31) if amount is not None else None,
        form="10-K" if amount is not None else None,
        period_end=PERIOD if amount is not None else None,
    )


def snapshot(
    *,
    price: float | None = 336.13,
    prior_revenue: str | None = "391035000000",
    **overrides: str | None,
) -> FundamentalSnapshot:
    """Apple's real FY2025 figures unless a test overrides one."""
    base: dict[str, str | None] = {
        "Revenues": None,
        "RevenueFromContractWithCustomerExcludingAssessedTax": "416161000000",
        "NetIncomeLoss": "112010000000",
        "OperatingIncomeLoss": "133050000000",
        "EarningsPerShareBasic": "7.49",
        "EarningsPerShareDiluted": "7.46",
        "Assets": "359241000000",
        "Liabilities": "285508000000",
        "StockholdersEquity": "73733000000",
        "CashAndCashEquivalentsAtCarryingValue": "39544000000",
    }
    base.update(overrides)

    return FundamentalSnapshot(
        instrument_id=1,
        asof=ASOF,
        price=price,
        currency="USD",
        values={k: value(k, v) for k, v in base.items()},
        prior_year={
            "RevenueFromContractWithCustomerExcludingAssessedTax": value(
                "RevenueFromContractWithCustomerExcludingAssessedTax", prior_revenue
            ),
            "Revenues": value("Revenues", None),
        },
    )


def metric(factor: object, name: str) -> object | None:
    return next((m for m in factor.metrics if m.name == name), None)  # type: ignore[attr-defined]


def run(snap: FundamentalSnapshot, params: FundamentalParams | None = None):  # type: ignore[no-untyped-def]
    return FundamentalEngine(params).evaluate(snap, requested_weight=0.4, provenance=FRESH)


class TestRatios:
    def test_price_to_earnings(self) -> None:
        factor, _ = run(snapshot())
        per = metric(factor, "P/E")

        assert per is not None
        assert per.raw == pytest.approx(336.13 / 7.49, rel=1e-6)  # type: ignore[attr-defined]

    def test_return_on_equity(self) -> None:
        """Apple's ROE genuinely exceeds 100% — buybacks have shrunk equity."""
        factor, _ = run(snapshot())
        roe = metric(factor, "ROE")

        assert roe is not None
        assert roe.raw == pytest.approx(151.9, abs=0.1)  # type: ignore[attr-defined]

    def test_debt_ratio(self) -> None:
        factor, _ = run(snapshot())
        debt = metric(factor, "Debt ratio")

        assert debt is not None
        assert debt.raw == pytest.approx(79.5, abs=0.1)  # type: ignore[attr-defined]

    def test_operating_margin(self) -> None:
        factor, _ = run(snapshot())
        margin = metric(factor, "Operating margin")

        assert margin is not None
        assert margin.raw == pytest.approx(32.0, abs=0.1)  # type: ignore[attr-defined]

    def test_revenue_growth_compares_against_a_year_ago(self) -> None:
        factor, _ = run(snapshot())
        growth = metric(factor, "Revenue growth")

        assert growth is not None
        assert growth.raw == pytest.approx(6.4, abs=0.1)  # type: ignore[attr-defined]


class TestRevenueTagFallback:
    """Both revenue tags are live, and picking the wrong one is silent.

    Apple's `Revenues` series ends in 2018 after it adopted ASC 606 and moved
    to `RevenueFromContractWithCustomerExcludingAssessedTax`. A margin computed
    against the stale tag would be wrong by the seven years of growth in
    between, and nothing would raise.
    """

    def test_the_asc606_tag_is_preferred(self) -> None:
        snap = snapshot(
            Revenues="265595000000",  # Apple's 2018 figure, still tagged
            RevenueFromContractWithCustomerExcludingAssessedTax="416161000000",
        )

        assert snap.revenue() == 416161000000.0

    def test_the_legacy_tag_is_used_when_it_is_the_only_one(self) -> None:
        snap = snapshot(
            Revenues="265595000000",
            RevenueFromContractWithCustomerExcludingAssessedTax=None,
        )

        assert snap.revenue() == 265595000000.0

    def test_margin_uses_the_current_denominator(self) -> None:
        snap = snapshot(Revenues="265595000000")
        factor, _ = run(snap)
        margin = metric(factor, "Operating margin")

        assert margin is not None
        # Against the 2018 tag this would read ~50%, which never happened.
        assert margin.raw == pytest.approx(32.0, abs=0.5)  # type: ignore[attr-defined]


class TestGuards:
    def test_a_loss_making_company_has_no_meaningful_pe(self) -> None:
        """Dividing by a negative EPS produces a negative P/E.

        Scored as "low is good" that would read as excellent value, which is
        the opposite of what it means.
        """
        factor, reasons = run(snapshot(EarningsPerShareBasic="-2.10"))
        per = metric(factor, "P/E")

        assert per is not None
        assert per.normalized == 0.0  # type: ignore[attr-defined]
        assert any("no meaningful P/E" in r.text for r in reasons)

    def test_negative_equity_does_not_produce_a_flattering_roe(self) -> None:
        """A loss over negative equity comes out positive."""
        factor, reasons = run(
            snapshot(NetIncomeLoss="-5000000000", StockholdersEquity="-1000000000")
        )
        roe = metric(factor, "ROE")

        assert roe is not None
        assert roe.normalized == 0.0  # type: ignore[attr-defined]
        assert any("not interpretable" in r.text for r in reasons)

    def test_zero_revenue_skips_the_margin_rather_than_dividing(self) -> None:
        factor, _ = run(
            snapshot(
                Revenues="0",
                RevenueFromContractWithCustomerExcludingAssessedTax="0",
            )
        )

        assert metric(factor, "Operating margin") is None

    def test_missing_price_skips_valuation_but_keeps_the_rest(self) -> None:
        factor, _ = run(snapshot(price=None))

        assert metric(factor, "P/E") is None
        assert metric(factor, "ROE") is not None
        assert factor.availability is Availability.AVAILABLE


class TestAbsenceIsCarried:
    def test_no_inputs_yields_unavailable_not_zero(self) -> None:
        """Scoring zero would be a judgement; there is nothing to judge."""
        snap = FundamentalSnapshot(
            instrument_id=1,
            asof=ASOF,
            price=100.0,
            currency="KRW",
            values={c: value(c, None) for c in ("Revenues", "NetIncomeLoss", "StockholdersEquity")},
        )

        factor, reasons = run(snap)

        assert factor.availability is Availability.UNAVAILABLE
        assert factor.effective_weight == 0.0
        assert factor.score == 0.0
        assert reasons == ()

    def test_the_reason_comes_from_the_lookup_not_the_engine(self) -> None:
        """So the wording a user sees is the one the repository produced."""
        snap = FundamentalSnapshot(
            instrument_id=1,
            asof=ASOF,
            price=100.0,
            currency="KRW",
            values={
                "Revenues": ReportedValue(
                    concept="Revenues",
                    value=None,
                    outcome="SOURCE_COVERAGE_UNAVAILABLE",
                    explanation="this source holds no data at all for this instrument",
                )
            },
        )

        factor, _ = run(snap)

        assert factor.availability_reason is not None
        assert "holds no data at all" in factor.availability_reason

    def test_a_partial_snapshot_still_scores_what_it_can(self) -> None:
        """Missing one input must not discard the others."""
        factor, _ = run(snapshot(OperatingIncomeLoss=None, EarningsPerShareBasic=None))

        assert factor.availability is Availability.AVAILABLE
        assert metric(factor, "ROE") is not None
        assert metric(factor, "Operating margin") is None
        assert metric(factor, "P/E") is None


class TestParamsAreStrategy:
    def test_moving_the_ideal_multiple_changes_the_score(self) -> None:
        """Which is why these live in params, not as constants."""
        strict, _ = run(snapshot(), FundamentalParams(per_ideal=15.0))
        lenient, _ = run(snapshot(), FundamentalParams(per_ideal=45.0))

        strict_per = metric(strict, "P/E")
        lenient_per = metric(lenient, "P/E")

        assert strict_per is not None and lenient_per is not None
        assert lenient_per.normalized > strict_per.normalized  # type: ignore[attr-defined]

    def test_the_engine_reports_its_own_identity(self) -> None:
        factor, _ = run(snapshot())
        assert factor.engine is Engine.FUNDAMENTAL
