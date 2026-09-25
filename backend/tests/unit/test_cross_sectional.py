"""Ranking a ratio against its market, and refusing to when there is no market.

`percentile_rank` existed for the whole of Phase 1 to 3 with nothing calling
it, while `normalize`'s own docstring said cross-sectional percentile was the
default. These tests exist so that claim is a check.

What they pin, in order of how quietly each would break:

* A rank and a fixed scale are different measurements, and the metric says
  which one produced it. A percentile that silently became a `bounded` call
  because the population was thin would report a considered position and mean
  "there was nobody to compare against".
* Only the monotonic ratios rank. Valuation keeps its absolute opinion,
  because a rank over raw multiples can only say "cheapest is best", which is
  the rule the engine already declines to follow.
* A population and the value being ranked within it come from one function.
  Assembled separately they drift, and a rank taken under eligibility rules
  that differ from the score's describes nothing while looking entirely
  ordinary.
* A figure that is not interpretable stays out of the population. Negative
  equity is the case: a loss over negative equity comes out positive, and one
  such peer would drag every other company's rank toward it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.normalize import bounded
from app.core.types import Availability, DataProvenance, Freshness, Metric
from app.engines.fundamental import (
    CROSS_SECTIONAL,
    FundamentalEngine,
    FundamentalParams,
    FundamentalSnapshot,
    PeerRatios,
    ReportedValue,
    comparable_ratios,
)

ASOF = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
PERIOD = date(2025, 12, 31)

FRESH = DataProvenance(
    source_asof=ASOF,
    source_checked_at=ASOF,
    data_age=timedelta(0),
    freshness=Freshness.FRESH,
)


def _value(concept: str, amount: float | None) -> ReportedValue:
    present = amount is not None
    return ReportedValue(
        concept=concept,
        value=Decimal(str(amount)) if present else None,
        outcome="FOUND" if present else "NO_OBSERVATION_IN_SOURCE",
        explanation="present" if present else "the source holds nothing here",
        filed_at=date(2026, 3, 30) if present else None,
        form="10-K" if present else None,
        period_end=PERIOD if present else None,
    )


def company(
    instrument_id: int = 1,
    *,
    roe: float = 0.10,
    debt: float = 0.50,
    margin: float = 0.10,
    growth: float = 0.10,
    eps: float = 5.0,
    price: float | None = 75.0,
    equity: float = 100.0,
) -> FundamentalSnapshot:
    """A company stated by the four ratios that get ranked, plus a P/E.

    Built from the ratios rather than from real filings on purpose: these
    tests are about what happens to a value once it is computed, and deriving
    the inputs keeps each case readable as the ratio it is testing.
    """
    revenue = 500.0
    values = {
        "Revenues": None,
        "RevenueFromContractWithCustomerExcludingAssessedTax": revenue,
        "NetIncomeLoss": roe * equity,
        "OperatingIncomeLoss": margin * revenue,
        "EarningsPerShareBasic": eps,
        "EarningsPerShareDiluted": eps,
        "Assets": 1000.0,
        "Liabilities": debt * 1000.0,
        "StockholdersEquity": equity,
        "CashAndCashEquivalentsAtCarryingValue": 40.0,
    }
    return FundamentalSnapshot(
        instrument_id=instrument_id,
        asof=ASOF,
        price=price,
        currency="USD",
        values={k: _value(k, v) for k, v in values.items()},
        prior_year={
            "RevenueFromContractWithCustomerExcludingAssessedTax": _value(
                "RevenueFromContractWithCustomerExcludingAssessedTax", revenue / (1.0 + growth)
            ),
            "Revenues": _value("Revenues", None),
        },
        anchor_period_end=PERIOD,
    )


def score(
    snap: FundamentalSnapshot,
    peers: PeerRatios | None = None,
    params: FundamentalParams | None = None,
) -> dict[str, Metric]:
    factor, _ = FundamentalEngine(params).evaluate(
        snap, requested_weight=0.4, provenance=FRESH, peers=peers
    )
    return {m.name: m for m in factor.metrics}


def market(roes: list[float]) -> tuple[list[FundamentalSnapshot], PeerRatios]:
    members = [company(i, roe=r) for i, r in enumerate(roes, start=1)]
    return members, PeerRatios.of(ASOF, members)


class TestRankingHappens:
    def test_rank_and_fixed_scale_are_different_measurements(self) -> None:
        """The best of a weak market ranks high and maps low.

        Every company here earns between 1% and 5% on equity, so the fixed
        scale calls all of them poor - correctly, against an absolute view of
        what a good return is. The rank says something else and equally true:
        one of them is the best of the five.
        """
        members, peers = market([0.01, 0.02, 0.03, 0.04, 0.05])
        best = members[-1]

        fixed = score(best)["ROE"]
        ranked = score(best, peers)["ROE"]

        assert fixed.normalized == pytest.approx(bounded(0.05, 0.0, 0.30))
        assert fixed.normalized < 20.0
        assert ranked.normalized == pytest.approx(90.0)

    def test_raw_is_untouched_by_the_scale(self) -> None:
        """Ranking moves the position, never the reported figure."""
        members, peers = market([0.01, 0.02, 0.03, 0.04, 0.05])

        assert score(members[0])["ROE"].raw == pytest.approx(1.0)
        assert score(members[0], peers)["ROE"].raw == pytest.approx(1.0)

    def test_an_instrument_is_in_its_own_population(self) -> None:
        """Nine names produce a population of nine, not eight.

        Excluding self would hand the best performer in a small market a 100
        on the strength of one comparison fewer, and the midpoint convention
        already stops a self-comparison from being worth a full rank.
        """
        members, peers = market([0.02, 0.04, 0.06])

        assert len(peers.population("ROE")) == len(members)

    def test_identical_companies_all_sit_at_fifty(self) -> None:
        """A market with no spread ranks everyone in the middle, not at an end."""
        members, peers = market([0.08] * 6)

        assert score(members[0], peers)["ROE"].normalized == pytest.approx(50.0)


class TestDirection:
    def test_debt_ratio_ranks_inverted(self) -> None:
        """Most leveraged scores lowest, least leveraged highest."""
        members = [company(i, debt=d) for i, d in enumerate([0.2, 0.4, 0.6, 0.8, 0.9], start=1)]
        peers = PeerRatios.of(ASOF, members)

        least, most = members[0], members[-1]

        assert score(least, peers)["Debt ratio"].normalized == pytest.approx(90.0)
        assert score(most, peers)["Debt ratio"].normalized == pytest.approx(10.0)

    def test_every_ranked_metric_declares_a_direction(self) -> None:
        """A ratio can be ranked only if the engine knows which way is good."""
        assert set(CROSS_SECTIONAL) == {
            "ROE",
            "Debt ratio",
            "Operating margin",
            "Revenue growth",
        }
        assert all(isinstance(v, bool) for v in CROSS_SECTIONAL.values())


class TestValuationKeepsItsOpinion:
    def test_price_to_earnings_is_not_ranked(self) -> None:
        """P/E scores the same with a market as without one.

        A rank over raw multiples asserts that the cheapest name is the best
        one. The engine does not believe that - a very low multiple is as
        often distress as value - so valuation keeps the absolute scale and
        the belief stays where it can be read.
        """
        members = [company(i, eps=e) for i, e in enumerate([2.0, 4.0, 5.0, 8.0, 12.0], start=1)]
        peers = PeerRatios.of(ASOF, members)
        dearest = members[0]

        assert score(dearest)["P/E"].normalized == pytest.approx(
            score(dearest, peers)["P/E"].normalized
        )

    def test_valuation_is_absent_from_every_population(self) -> None:
        members, peers = market([0.05, 0.10, 0.15])

        assert "P/E" not in peers.values
        assert "P/E" not in comparable_ratios(members[0])


class TestThinPopulations:
    def test_below_the_floor_it_falls_back_to_the_fixed_scale(self) -> None:
        """Four peers is not a rank, and the score says the fixed scale ran.

        A percentile over four values can only return 12.5, 37.5, 62.5 or
        87.5. That reads as a considered position and is really the shape of
        having almost nothing to compare against.
        """
        members, peers = market([0.01, 0.02, 0.03, 0.04])
        assert len(peers.population("ROE")) == 4

        metric = score(members[-1], peers)["ROE"]

        assert metric.normalized == pytest.approx(bounded(0.04, 0.0, 0.30))
        assert metric.detail is not None
        assert "비교군이 4개뿐" in metric.detail

    def test_the_floor_is_a_parameter_of_the_strategy(self) -> None:
        """Lowering `min_peers` is a change to the rule, not to plumbing."""
        members, peers = market([0.01, 0.02, 0.03, 0.04])

        permissive = score(members[-1], peers, FundamentalParams(min_peers=4))["ROE"]

        assert permissive.normalized == pytest.approx(87.5)
        assert permissive.detail is not None
        assert "비교군 4개 중 순위" in permissive.detail


class TestTheScaleIsRecorded:
    def test_a_ranked_metric_names_its_population(self) -> None:
        members, peers = market([0.02, 0.04, 0.06, 0.08, 0.10])

        for name, metric in score(members[0], peers).items():
            assert metric.detail is not None
            if name in CROSS_SECTIONAL:
                assert "비교군 5개 중 순위" in metric.detail
            else:
                assert "고정 척도" in metric.detail

    def test_no_peer_group_is_stated_rather_than_implied(self) -> None:
        """A run with no universe is a different rule, and the row says so.

        Valuation reads plainly "fixed scale" in both cases, because its
        absolute scale is a belief rather than a fallback. Only a ratio that
        would have been ranked reports why it was not.
        """
        for name, metric in score(company()).items():
            assert metric.detail is not None
            if name in CROSS_SECTIONAL:
                assert "고정 척도, 비교군 없음" in metric.detail
            else:
                assert metric.detail.endswith("고정 척도")

    def test_without_peers_the_engine_scores_exactly_as_before(self) -> None:
        """Every run stored before ranking existed still reproduces.

        The normalized positions with no peer group are the `bounded` and
        `peak_at` values the engine has always produced, so a replay of an old
        run under this code returns the numbers that were stored.
        """
        metrics = score(company(roe=0.12, debt=0.55, margin=0.18, growth=0.25))

        assert metrics["ROE"].normalized == pytest.approx(bounded(0.12, 0.0, 0.30))
        assert metrics["Debt ratio"].normalized == pytest.approx(
            bounded(0.55, 0.2, 0.8, invert=True)
        )
        assert metrics["Operating margin"].normalized == pytest.approx(bounded(0.18, 0.0, 0.30))
        assert metrics["Revenue growth"].normalized == pytest.approx(bounded(0.25, -0.20, 0.40))


class TestUninterpretableFiguresStayOut:
    def test_negative_equity_scores_zero_and_leaves_the_population(self) -> None:
        """One such peer would drag every other company's rank toward it.

        A loss divided by negative equity comes out positive, so the figure is
        not merely bad - it points the wrong way. The engine still states an
        explicit zero for the company itself, because "not interpretable" is a
        judgement worth showing, but the number never joins a denominator.
        """
        healthy = [company(i, roe=r) for i, r in enumerate([0.05, 0.10, 0.15, 0.20], start=1)]
        broken = company(9, roe=-0.50, equity=-100.0)
        peers = PeerRatios.of(ASOF, [*healthy, broken])

        assert len(peers.population("ROE")) == 4
        assert "ROE" not in comparable_ratios(broken)

        metric = score(broken, peers)["ROE"]
        assert metric.normalized == 0.0
        assert metric.detail == "자본잠식"

    def test_a_company_missing_a_ratio_shrinks_only_that_population(self) -> None:
        members = [company(i, roe=r) for i, r in enumerate([0.05, 0.10, 0.15, 0.20], start=1)]
        without_growth = company(9)
        without_growth = FundamentalSnapshot(
            instrument_id=9,
            asof=ASOF,
            price=without_growth.price,
            currency="USD",
            values=without_growth.values,
            prior_year=None,
            anchor_period_end=PERIOD,
        )
        peers = PeerRatios.of(ASOF, [*members, without_growth])

        assert len(peers.population("ROE")) == 5
        assert len(peers.population("Revenue growth")) == 4


class TestOneSourceOfTruth:
    def test_the_population_agrees_with_what_was_scored(self) -> None:
        """A rank taken under different rules from the score describes nothing.

        The engine and `comparable_ratios` call the same functions, and this
        is what holds them there: every ranked metric the engine reports must
        appear in the contribution with the same value, and nothing else may.
        """
        snap = company(roe=0.11, debt=0.42, margin=0.23, growth=-0.05)
        contributed = comparable_ratios(snap)
        scored = score(snap)

        ranked_names = {n for n in scored if n in CROSS_SECTIONAL}
        assert set(contributed) == ranked_names

        for name, ratio in contributed.items():
            # Metrics carry percentages; a population carries plain ratios.
            assert scored[name].raw == pytest.approx(ratio * 100.0)

    def test_a_snapshot_with_nothing_to_say_contributes_nothing(self) -> None:
        empty = FundamentalSnapshot(
            instrument_id=1,
            asof=ASOF,
            price=None,
            currency="USD",
            values={},
        )
        assert comparable_ratios(empty) == {}

        peers = PeerRatios.of(ASOF, [empty])
        assert peers.population("ROE") == ()


class TestTheFactorStillGuardsItself:
    def test_ranking_does_not_bypass_the_coverage_floor(self) -> None:
        """Three ratios are still the minimum, ranked or not."""
        thin = FundamentalSnapshot(
            instrument_id=1,
            asof=ASOF,
            price=75.0,
            currency="USD",
            values={
                "NetIncomeLoss": _value("NetIncomeLoss", 10.0),
                "StockholdersEquity": _value("StockholdersEquity", 100.0),
            },
            anchor_period_end=PERIOD,
        )
        peers = PeerRatios.of(ASOF, [company(i) for i in range(1, 7)])

        factor, _ = FundamentalEngine().evaluate(
            thin, requested_weight=0.4, provenance=FRESH, peers=peers
        )

        assert factor.availability is Availability.UNAVAILABLE
        assert factor.effective_weight == 0.0
