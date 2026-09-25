"""The market regime from index closes: what counts as rising, as turbulent, and as unknown."""

from __future__ import annotations

import math

import pytest

from app.core.calendar import Market
from app.models.instrument import Listing
from app.scoring.regime import DEFAULT, Label, RegimeParams, _volatility, breadth, classify
from app.services.regime_service import index_for

N = DEFAULT.history_needed


def series(n: int, *, growth: float, wobble: list[float]) -> list[float]:
    """Closes compounding by `growth`, each nudged up or down by its own wobble in turn."""
    return [100 * (1 + growth) ** i * (1 + wobble[i] * (-1) ** i) for i in range(n)]


def calming(n: int) -> list[float]:
    """A wobble that shrinks over time: today's volatility is the year's lowest."""
    return [0.02 * (1 - i / n) + 0.001 for i in range(n)]


def storming(n: int) -> list[float]:
    """Calm, then the last weeks rough: today's volatility is the year's highest."""
    return [0.001 if i < n - 15 else 0.03 for i in range(n)]


class TestLabel:
    def test_too_little_history_is_unknown_not_a_guess(self) -> None:
        closes = series(N - 1, growth=0.001, wobble=calming(N - 1))
        regime = classify(closes)
        assert regime.label == Label.UNKNOWN
        assert regime.close == closes[-1] and regime.trend_gap is None

    def test_a_close_at_or_below_zero_is_unknown(self) -> None:
        closes = series(N, growth=0.001, wobble=calming(N))
        closes[10] = 0.0
        assert classify(closes).label == Label.UNKNOWN

    def test_rising_and_calm_is_risk_on(self) -> None:
        regime = classify(series(N, growth=0.002, wobble=calming(N)))
        assert regime.label == Label.RISK_ON
        assert regime.trend_gap is not None and regime.trend_gap > 0
        assert regime.volatility_rank == 0.0

    def test_falling_and_turbulent_is_risk_off(self) -> None:
        regime = classify(series(N, growth=-0.002, wobble=storming(N)))
        assert regime.label == Label.RISK_OFF
        assert regime.volatility_rank == 1.0

    def test_rising_but_turbulent_is_neutral(self) -> None:
        assert classify(series(N, growth=0.002, wobble=storming(N))).label == Label.NEUTRAL

    def test_falling_but_calm_is_neutral(self) -> None:
        assert classify(series(N, growth=-0.002, wobble=calming(N))).label == Label.NEUTRAL

    def test_a_year_of_unchanging_volatility_is_not_the_highest(self) -> None:
        # Every 20-session window has the same spread: today is ordinary.
        closes = [100.0 * (1.01 if i % 2 else 1.0) for i in range(N)]
        regime = classify(closes)
        assert regime.volatility_rank is not None and regime.volatility_rank < 0.5

    def test_the_threshold_is_a_parameter(self) -> None:
        closes = series(N, growth=0.002, wobble=storming(N))
        loose = RegimeParams(high_volatility_rank=1.01)
        assert classify(closes, loose).label == Label.RISK_ON


class TestMeasures:
    def test_trend_gap_and_twenty_day_return(self) -> None:
        closes = [100.0] * (N - 1) + [110.0]
        regime = classify(closes)
        window = closes[-DEFAULT.trend_window :]
        assert regime.trend_gap == pytest.approx(110 / (sum(window) / len(window)) - 1)
        assert regime.return_20d == pytest.approx(0.1)

    def test_volatility_is_annualised_from_log_returns(self) -> None:
        a = math.log(1.1)
        closes = [100.0 * (1.1 if i % 2 else 1.0) for i in range(21)]
        # Twenty returns alternating +a and -a: mean 0, sample deviation a*sqrt(20/19).
        assert _volatility(closes) == pytest.approx(a * math.sqrt(20 / 19) * math.sqrt(252))


class TestBreadth:
    def test_share_above_their_own_average(self) -> None:
        up = [1.0] * 49 + [2.0]
        down = [2.0] * 49 + [1.0]
        assert breadth([up, up, down]) == (pytest.approx(2 / 3), 3)

    def test_a_name_without_enough_history_is_not_measured(self) -> None:
        up = [1.0] * 49 + [2.0]
        assert breadth([up, [1.0] * 10]) == (1.0, 1)

    def test_nothing_measured_is_none_not_zero(self) -> None:
        assert breadth([[1.0] * 10]) == (None, 0)


class TestWhichIndex:
    @pytest.mark.parametrize(
        ("market", "listing", "code"),
        [
            (Market.KR, Listing.KOSDAQ, "^KQ11"),
            (Market.KR, Listing.KOSPI, "^KS11"),
            (Market.KR, None, "^KS11"),
            (Market.US, Listing.NASDAQ, "^GSPC"),
            (Market.US, None, "^GSPC"),
        ],
    )
    def test_each_name_is_read_against_its_own_board(
        self, market: Market, listing: Listing | None, code: str
    ) -> None:
        assert index_for(market, listing) == code


class TestBreadthAtAMoment:
    def test_a_name_promoted_later_is_not_counted_and_bars_are_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from app.services import regime_service

        asof = datetime(2026, 9, 23, 6, 30, tzinfo=UTC)
        names = [SimpleNamespace(instrument_id=n) for n in (1, 2, 3)]
        asked: list[tuple[int, object]] = []

        def history(_s: object, n: int, _i: object, *, limit: int, available_before: object):  # type: ignore[no-untyped-def]
            asked.append((n, available_before))
            closes = [1.0] * 49 + ([2.0] if n == 1 else [0.5])
            return [SimpleNamespace(close=c) for c in closes]

        monkeypatch.setattr(regime_service.instrument_repo, "list_active", lambda *a, **k: names)
        monkeypatch.setattr(regime_service.promotion_repo, "promoted_after", lambda *a: {3})
        monkeypatch.setattr(regime_service.candle_repo, "history", history)

        assert regime_service.breadth_at(None, Market.KR, asof) == (0.5, 2)  # type: ignore[arg-type]
        assert sorted(n for n, _ in asked) == [1, 2]
        assert all(bound == asof for _, bound in asked)
