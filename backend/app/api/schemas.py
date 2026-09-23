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
from typing import Any

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


class OverlayEventOut(BaseModel):
    event_type: str
    first_at: datetime
    articles: int
    sentiment: float
    intensity: float
    confidence: float
    decay: float
    contribution: float
    title: str


class OverlayOut(BaseModel):
    """News events beside the score. Not part of `total_score` or `action`."""

    points: float = Field(description="Bounded; what the news of the moment would add")
    events: int
    readings_used: int
    unread_articles: int = Field(
        description="Confirmed articles with no reading yet; the overlay lags by this much"
    )
    news_freshness: str
    asof: datetime
    overlay_version: int
    top_events: list[OverlayEventOut]


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
    overlay: OverlayOut | None = None

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


class BacktestWindowOut(BaseModel):
    """One measurement, with the caveats that qualify it.

    `abstained`, `without_data` and `unfilled` travel beside the returns
    rather than in a footnote. A window whose strategy declined to judge for
    half its sessions produced a number that means something different from
    one that traded throughout, and a screen that shows only the return cannot
    say which it is looking at.
    """

    window_index: int
    sample_type: str
    period_start: date
    period_end: date
    strategy: str
    strategy_params: dict[str, Any]

    sessions: int
    observations: int
    total_return: float | None
    cagr: float | None
    max_drawdown: float | None
    sharpe: float | None
    win_rate: float | None
    profit_factor: float | None

    trades: int
    abstained: int
    without_data: int
    unfilled: int


class BacktestRunSummary(BaseModel):
    """Enough to list a run and tell it apart from its neighbours."""

    id: int
    instrument_id: int
    symbol: str
    name: str
    strategy_kind: str
    strategy_version: str
    strategy_params: dict[str, Any]
    fitter_version: str | None
    period_start: date
    period_end: date
    started_at: datetime
    windows: int
    has_holdout: bool


class BacktestRunDetail(BacktestRunSummary):
    """Every coordinate a reproduction would need.

    The screen's job is to show that a result can be checked, not to
    summarise it — so the commit, the data snapshot, the costs that were
    applied and the fingerprints are all here rather than hidden behind a
    developer tool.
    """

    market: str
    interval: str

    strategy_fingerprint: str
    fit_trace_fingerprint: str
    holdout_strategy_fingerprint: str | None

    git_commit_sha: str
    git_dirty: bool
    data_snapshot_at: datetime

    starting_cash: float
    commission_bps: float
    slippage_bps: float
    min_commission: float
    execution_model: str
    bar_minutes: int | None
    universe: list[int] | None
    """Instrument ids the fundamental ratios were ranked against, or null.

    Null means no ranking happened and every ratio used its fixed scale. It
    belongs in this payload because the screen's job is to open every
    coordinate a reproduction needs, and two runs identical in all the others
    still differ here.
    """

    train_sessions: int
    eval_sessions: int
    anchored: bool
    require_complete_sessions: bool
    holdout_start: date | None
    holdout_end: date | None

    window_rows: list[BacktestWindowOut]
