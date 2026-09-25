"""Technical factor engine.

Takes a price series and returns a `Factor` carrying every intermediate value,
so the dashboard can show raw -> normalized -> weight -> contribution rather
than asserting a score.

**This module never touches the database.** It receives a `PriceSeries` and
returns a value; the import-linter contract forbids it from importing
SQLAlchemy or the ORM. That is not decoration: the point-in-time filter lives
in the repository layer, and an engine that could query directly could bypass
it and see the future without any test noticing.

It also never reads a clock. `asof` is passed in. The same call with the same
inputs must produce the same output forever, or the backtest stops being
evidence about the live system.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.indicators import (
    bollinger_bands,
    macd,
    percent_distance,
    relative_strength_index,
    simple_moving_average,
    zscore,
)
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


@dataclass(frozen=True, slots=True)
class PriceSeries:
    """Closing prices and volumes, oldest first.

    A plain value rather than an ORM row, so the engine stays ignorant of
    persistence and can be exercised from a literal list in tests.
    """

    instrument_id: int
    closes: tuple[float, ...]
    volumes: tuple[float, ...]
    asof: datetime

    def __post_init__(self) -> None:
        if len(self.closes) != len(self.volumes):
            raise ValueError(
                f"closes and volumes must align; got {len(self.closes)} and {len(self.volumes)}"
            )

    @property
    def last_close(self) -> float | None:
        return self.closes[-1] if self.closes else None


@dataclass(frozen=True, slots=True)
class TechnicalParams:
    """Tunable inputs. Part of the strategy version, not constants.

    Changing `rsi_period` from 14 to 21 produces different signals from
    identical data, which makes it a strategy change and not a refactor.
    """

    rsi_period: int = 14
    ma_short: int = 20
    ma_long: int = 60
    volume_period: int = 20
    bollinger_period: int = 20
    bollinger_std: float = 2.0

    # RSI is scored as a distance from neutral rather than monotonically:
    # both 85 and 15 are notable, and scoring high-is-good would rate an
    # overbought instrument as the best possible buy.
    rsi_ideal: float = 55.0
    rsi_tolerance: float = 45.0

    @property
    def min_bars(self) -> int:
        """Shortest history that lets every metric compute."""
        return max(self.ma_long, self.rsi_period + 1, self.bollinger_period, self.volume_period)


class TechnicalEngine:
    """Scores price action. Implements the `FactorEngine` protocol."""

    engine = Engine.TECHNICAL

    def __init__(self, params: TechnicalParams | None = None) -> None:
        self.params = params or TechnicalParams()

    def evaluate(
        self,
        series: PriceSeries,
        *,
        requested_weight: float,
        provenance: DataProvenance,
    ) -> tuple[Factor, tuple[SignalReason, ...]]:
        """Compute the factor and the evidence lines that explain it.

        Returns the reasons alongside the factor so that the wording shown in
        the UI and the arithmetic stored in the database come from the same
        place and cannot drift apart.
        """
        p = self.params
        closes = list(series.closes)
        price = series.last_close

        if price is None or len(closes) < p.min_bars:
            return self._insufficient(series, requested_weight, provenance, len(closes))

        metrics: list[Metric] = []
        reasons: list[SignalReason] = []

        # --- RSI ---------------------------------------------------------
        rsi = relative_strength_index(closes, p.rsi_period)
        if rsi is not None:
            score = peak_at(rsi, p.rsi_ideal, p.rsi_tolerance)
            metrics.append(Metric("RSI", raw=rsi, normalized=score, detail=f"{p.rsi_period}일"))
            reasons.append(self._rsi_reason(rsi))

        # --- distance from the short moving average ----------------------
        ma_short = simple_moving_average(closes, p.ma_short)
        if ma_short is not None:
            distance = percent_distance(price, ma_short)
            if distance is not None:
                # +/-10% spans the useful range; beyond that is already extended.
                score = bounded(distance, -10.0, 10.0)
                metrics.append(
                    Metric(
                        f"MA{p.ma_short} distance",
                        raw=distance,
                        normalized=score,
                        detail=f"현재가 대비 {p.ma_short}일 평균",
                    )
                )
                reasons.append(self._ma_reason(distance, p.ma_short))

        # --- trend: short average above long ------------------------------
        ma_long = simple_moving_average(closes, p.ma_long)
        if ma_short is not None and ma_long is not None:
            spread = percent_distance(ma_short, ma_long)
            if spread is not None:
                score = bounded(spread, -10.0, 10.0)
                metrics.append(
                    Metric(f"MA{p.ma_short}/MA{p.ma_long} spread", raw=spread, normalized=score)
                )
                reasons.append(self._trend_reason(spread, p.ma_short, p.ma_long))

        # --- volume, relative to its own recent normal --------------------
        volume_z = zscore(list(series.volumes), p.volume_period)
        if volume_z is not None:
            score = bounded(volume_z, -2.0, 3.0)
            metrics.append(
                Metric(
                    "Volume z-score",
                    raw=volume_z,
                    normalized=score,
                    detail=f"{p.volume_period}일 평균 대비",
                )
            )
            reasons.append(self._volume_reason(volume_z))

        # --- position within the Bollinger band ---------------------------
        bands = bollinger_bands(closes, p.bollinger_period, p.bollinger_std)
        if bands is not None:
            position = bands.position(price)
            # Mid-band is unremarkable; the edges carry the information.
            score = clamp_score(position * 100.0)
            metrics.append(
                Metric(
                    "Bollinger position", raw=position, normalized=score, detail="0=하단, 1=상단"
                )
            )

        # --- MACD ---------------------------------------------------------
        macd_result = macd(closes)
        if macd_result is not None:
            # Scaled by price so the raw magnitude is comparable across a
            # 260,000 KRW stock and a 336 USD one.
            relative = macd_result.histogram / price * 100.0
            score = bounded(relative, -2.0, 2.0)
            metrics.append(Metric("MACD histogram", raw=macd_result.histogram, normalized=score))
            reasons.append(self._macd_reason(macd_result.histogram))

        if not metrics:
            return self._insufficient(series, requested_weight, provenance, len(closes))

        factor_score = sum(m.normalized for m in metrics) / len(metrics)

        factor = Factor(
            engine=Engine.TECHNICAL,
            score=clamp_score(factor_score),
            metrics=tuple(metrics),
            requested_weight=requested_weight,
            effective_weight=requested_weight,
            availability=Availability.AVAILABLE,
            provenance=provenance,
        )
        return factor, tuple(reasons)

    # --- evidence lines ---------------------------------------------------
    # Each returns a rendered sentence plus its status, produced here rather
    # than in the frontend so the displayed claim always matches the number
    # that was actually computed. The sentences are for the reader, who reads
    # Korean; metric names stay as they are, since code keys on them.

    @staticmethod
    def _rsi_reason(rsi: float) -> SignalReason:
        if rsi >= 70:
            return SignalReason(
                ReasonStatus.OPPOSES, f"RSI {rsi:.1f} — 과매수", Engine.TECHNICAL, "RSI"
            )
        if rsi <= 30:
            return SignalReason(
                ReasonStatus.OPPOSES, f"RSI {rsi:.1f} — 과매도", Engine.TECHNICAL, "RSI"
            )
        return SignalReason(
            ReasonStatus.SUPPORTS,
            f"RSI {rsi:.1f} — 극단 구간 아님",
            Engine.TECHNICAL,
            "RSI",
        )

    @staticmethod
    def _ma_reason(distance: float, period: int) -> SignalReason:
        if distance > 1.0:
            return SignalReason(
                ReasonStatus.SUPPORTS,
                f"{period}일 평균보다 {distance:+.1f}% 위에서 거래",
                Engine.TECHNICAL,
                f"MA{period} distance",
            )
        if distance < -1.0:
            return SignalReason(
                ReasonStatus.OPPOSES,
                f"{period}일 평균보다 {distance:+.1f}% 아래에서 거래",
                Engine.TECHNICAL,
                f"MA{period} distance",
            )
        return SignalReason(
            ReasonStatus.NEUTRAL,
            f"{period}일 평균 부근 ({distance:+.1f}%)",
            Engine.TECHNICAL,
            f"MA{period} distance",
        )

    @staticmethod
    def _trend_reason(spread: float, short: int, long: int) -> SignalReason:
        if spread > 0:
            return SignalReason(
                ReasonStatus.SUPPORTS,
                f"{short}일선이 {long}일선보다 {spread:+.1f}% 위 — 상승 추세",
                Engine.TECHNICAL,
            )
        return SignalReason(
            ReasonStatus.OPPOSES,
            f"{short}일선이 {long}일선보다 {spread:+.1f}% 아래 — 하락 추세",
            Engine.TECHNICAL,
        )

    @staticmethod
    def _volume_reason(z: float) -> SignalReason:
        if z >= 1.0:
            return SignalReason(
                ReasonStatus.SUPPORTS,
                f"거래량이 20일 평소보다 {z:+.1f} 표준편차 많음",
                Engine.TECHNICAL,
                "Volume z-score",
            )
        if z <= -1.0:
            return SignalReason(
                ReasonStatus.OPPOSES,
                f"거래량이 20일 평소보다 {z:+.1f} 표준편차 적음",
                Engine.TECHNICAL,
                "Volume z-score",
            )
        return SignalReason(
            ReasonStatus.NEUTRAL, f"거래량 평소 수준 ({z:+.1f} 표준편차)", Engine.TECHNICAL
        )

    @staticmethod
    def _macd_reason(histogram: float) -> SignalReason:
        if histogram > 0:
            return SignalReason(
                ReasonStatus.SUPPORTS, "MACD 히스토그램 양수", Engine.TECHNICAL, "MACD histogram"
            )
        return SignalReason(
            ReasonStatus.OPPOSES, "MACD 히스토그램 음수", Engine.TECHNICAL, "MACD histogram"
        )

    def _insufficient(
        self,
        series: PriceSeries,
        requested_weight: float,
        provenance: DataProvenance,
        available: int,
    ) -> tuple[Factor, tuple[SignalReason, ...]]:
        """Not enough history to compute anything honestly.

        Returns UNAVAILABLE with a reason rather than a score derived from a
        short window. A 60-day average computed from 12 bars is not a slightly
        noisier average; it is a different statistic with the same label.
        """
        reason = (
            f"일봉이 {available}개뿐입니다. {self.params.ma_long}일선에는 "
            f"{self.params.min_bars}개가 필요합니다"
        )
        factor = Factor(
            engine=Engine.TECHNICAL,
            score=0.0,
            metrics=(),
            requested_weight=requested_weight,
            effective_weight=0.0,
            availability=Availability.UNAVAILABLE,
            provenance=provenance,
            availability_reason=reason,
        )
        return factor, ()
