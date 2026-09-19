"""API response shapes.

Pydantic lives here at the edge, not in the domain. The ORM rows and the
frozen dataclasses inside `core` stay free of serialisation concerns; these
models exist only to describe what goes over the wire.

The factor payload deliberately carries every intermediate number — raw,
normalized, both weights and the contribution. The drawer in the UI renders
exactly these fields, so what a person reads on screen is the arithmetic that
was actually stored rather than a summary recomputed in the browser.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class MetricOut(BaseModel):
    name: str
    raw: float
    normalized: float = Field(description="0-100 position after normalization")
    detail: str | None = None


class FactorOut(BaseModel):
    engine: str
    score: float
    metrics: list[MetricOut]

    requested_weight: float = Field(description="What the strategy config asked for")
    effective_weight: float = Field(description="What was actually applied")
    contribution: float = Field(description="score * effective_weight")

    availability: str
    availability_reason: str | None = None

    source_asof: datetime | None = None
    source_checked_at: datetime | None = Field(
        default=None,
        description=(
            "When the source was last reached. For fundamentals this, not the "
            "age of the filing, decides usability."
        ),
    )
    freshness_status: str


class ReasonOut(BaseModel):
    status: str = Field(description="SUPPORTS, NEUTRAL or OPPOSES")
    text: str
    engine: str
    metric_name: str | None = None


class SignalOut(BaseModel):
    id: int
    instrument_id: int
    symbol: str
    name: str
    market: str

    total_score: float
    action: str

    data_asof: datetime = Field(description="The data this was computed from")
    decision_at: datetime = Field(description="When the judgement was finalised")
    earliest_execution_at: datetime = Field(
        description=(
            "Soonest an order could honestly fill. Named 'earliest' because "
            "this system places no orders — nothing here was ever filled."
        )
    )

    strategy_version: str
    policy: str
    abstained_reason: str | None = None

    factors: list[FactorOut]
    reasons: list[ReasonOut]

    @property
    def effective_weight_total(self) -> float:
        return sum(f.effective_weight for f in self.factors)


class CandleOut(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class InstrumentOut(BaseModel):
    instrument_id: int
    symbol: str
    name: str
    market: str
    sector: str | None = None
    currency: str

    last_close: float | None = None
    last_close_date: date | None = None
    change_pct: float | None = None


class FxBreakdownOut(BaseModel):
    """A US position's return split into its two sources.

    Without this split a KRW-denominated return on a foreign holding cannot be
    interpreted: a gain might be the stock rising, the won weakening, or one
    offsetting the other.
    """

    krw_pnl: float
    krw_pnl_pct: float
    local_pnl: float
    local_pnl_pct: float = Field(description="The part that came from the stock")
    fx_pnl_krw: float = Field(description="The part that came from the exchange rate")
    fx_pnl_pct: float
    avg_fx_rate: float
    current_fx_rate: float
    currency: str


class HoldingOut(BaseModel):
    instrument_id: int
    symbol: str
    name: str
    market: str
    quantity: float
    avg_price: float
    currency: str
    last_close: float | None = None
    market_value_krw: float | None = None
    fx: FxBreakdownOut | None = None


class PortfolioOut(BaseModel):
    total_value_krw: float
    invested_krw: float
    cash_krw: float
    pnl_krw: float
    pnl_pct: float
    holdings: list[HoldingOut]
    is_live: bool = Field(
        description=(
            "False when no broker credentials are configured, so the figures "
            "are not from a real account."
        )
    )
    note: str | None = None
