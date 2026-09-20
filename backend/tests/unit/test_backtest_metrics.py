"""Metrics, and the cases where the honest answer is no number at all.

Known inputs with hand-checkable expectations, plus the refusals. The refusals
carry as much weight as the arithmetic: a Sharpe ratio from four days, or a
profit factor from a run that never lost, is the kind of figure this project
keeps finding — plausible, precise, and meaningless.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.backtest.metrics import (
    MIN_OBSERVATIONS,
    ClosedTrade,
    EquityPoint,
    cagr,
    daily_returns,
    max_drawdown,
    profit_factor,
    sharpe,
    summarise,
    total_return,
    win_rate,
)

START = date(2024, 1, 1)


def curve(values: list[str], *, step_days: int = 1) -> list[EquityPoint]:
    return [
        EquityPoint(day=START + timedelta(days=i * step_days), value=Decimal(v))
        for i, v in enumerate(values)
    ]


def trade(pnl: str) -> ClosedTrade:
    return ClosedTrade(instrument_id=1, entry_at=START, exit_at=START, pnl=Decimal(pnl))


class TestReturns:
    def test_total_return_is_end_over_start(self) -> None:
        assert total_return(curve(["100", "110", "121"])) == pytest.approx(0.21)

    def test_daily_returns_are_between_consecutive_points(self) -> None:
        assert daily_returns(curve(["100", "110", "121"])) == pytest.approx([0.1, 0.1])

    def test_one_point_is_not_a_return(self) -> None:
        assert total_return(curve(["100"])) is None

    def test_a_wiped_out_day_yields_no_return_rather_than_a_clamped_zero(self) -> None:
        """A clamped zero would count as a calm day and flatter every
        dispersion figure that follows."""
        assert daily_returns(curve(["100", "0", "0"])) == pytest.approx([-1.0])


class TestCagr:
    def test_doubling_over_a_year_is_about_one_hundred_percent(self) -> None:
        points = [
            EquityPoint(day=date(2024, 1, 1), value=Decimal("100")),
            EquityPoint(day=date(2025, 1, 1), value=Decimal("200")),
        ]
        assert cagr(points) == pytest.approx(1.0, abs=0.01)

    def test_doubling_over_two_years_is_about_forty_one_percent(self) -> None:
        points = [
            EquityPoint(day=date(2024, 1, 1), value=Decimal("100")),
            EquityPoint(day=date(2026, 1, 1), value=Decimal("200")),
        ]
        assert cagr(points) == pytest.approx(0.4142, abs=0.005)

    def test_a_span_under_a_month_is_not_annualised(self) -> None:
        """Three good days become a triple-digit CAGR, which means nothing."""
        points = [
            EquityPoint(day=date(2024, 1, 1), value=Decimal("100")),
            EquityPoint(day=date(2024, 1, 4), value=Decimal("103")),
        ]
        assert cagr(points) is None


class TestDrawdown:
    def test_it_measures_peak_to_trough_not_start_to_trough(self) -> None:
        """Rising to 150 then falling to 75 is -50%, not -25%."""
        assert max_drawdown(curve(["100", "150", "75", "120"])) == pytest.approx(-0.5)

    def test_a_curve_that_only_rises_has_no_drawdown(self) -> None:
        assert max_drawdown(curve(["100", "110", "120"])) == 0.0

    def test_the_trough_counts_even_if_it_recovers(self) -> None:
        assert max_drawdown(curve(["100", "50", "100"])) == pytest.approx(-0.5)


class TestSharpe:
    def test_too_few_observations_is_refused(self) -> None:
        assert sharpe([0.01] * (MIN_OBSERVATIONS - 1)) is None

    def test_a_constant_series_has_no_ratio(self) -> None:
        """Zero variance is undefined, not infinite."""
        assert sharpe([0.01] * (MIN_OBSERVATIONS + 5)) is None

    def test_a_known_series(self) -> None:
        returns = [0.01, -0.01] * 20
        result = sharpe(returns)
        assert result is not None
        assert result == pytest.approx(0.0, abs=1e-9)

    def test_it_annualises_by_the_square_root_of_trading_days(self) -> None:
        returns = [0.02, 0.01] * 20
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        expected = mean / math.sqrt(variance) * math.sqrt(252)

        assert sharpe(returns) == pytest.approx(expected)

    def test_the_risk_free_rate_is_subtracted(self) -> None:
        """At 4% a year, ignoring it hands a strategy roughly 0.2 for free."""
        returns = [0.002, 0.001] * 20
        plain = sharpe(returns)
        adjusted = sharpe(returns, risk_free_annual=0.04)

        assert plain is not None and adjusted is not None
        assert adjusted < plain


class TestTradeStatistics:
    def test_win_rate(self) -> None:
        assert win_rate([trade("10"), trade("-5"), trade("3"), trade("-1")]) == 0.5

    def test_a_break_even_trade_is_not_a_win(self) -> None:
        """It paid its costs and returned nothing."""
        assert win_rate([trade("10"), trade("0")]) == 0.5

    def test_profit_factor(self) -> None:
        assert profit_factor([trade("30"), trade("-10"), trade("-5")]) == pytest.approx(2.0)

    def test_a_run_that_never_lost_has_no_profit_factor(self) -> None:
        """An infinity here reads as skill."""
        assert profit_factor([trade("10"), trade("20")]) is None

    def test_no_trades_yields_nothing(self) -> None:
        assert win_rate([]) is None
        assert profit_factor([]) is None


class TestSummary:
    def test_a_short_run_reports_its_sample_and_withholds_the_rest(self) -> None:
        result = summarise(curve(["100", "101", "102"]), [trade("2")])

        assert result is not None
        assert result.observations == 2
        assert result.is_reportable is False
        assert result.sharpe is None
        assert result.total_return == pytest.approx(0.02)

    def test_a_long_enough_run_reports_dispersion(self) -> None:
        values = [str(100 + (i % 7)) for i in range(60)]
        result = summarise(curve(values), [trade("5"), trade("-2")])

        assert result is not None
        assert result.is_reportable is True
        assert result.sharpe is not None
        assert result.trades == 2

    def test_a_single_point_is_not_a_run(self) -> None:
        assert summarise(curve(["100"]), []) is None
