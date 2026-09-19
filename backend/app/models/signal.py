"""Signals and their factor decomposition.

`SignalFactor` is what makes a score defensible. Storing only the total means
"why 59.7?" can never be answered after the fact; storing the raw measurement,
its normalized position, the weight asked for, the weight actually applied and
the resulting contribution means the arithmetic can be replayed from rows alone.

Note the three clocks on `Signal`. They are not redundant:

    data_asof              the data it was computed from (the 09/19 close)
    decision_at            when the judgement was finalised (after that close)
    earliest_execution_at  the soonest an order could honestly fill (09/22 open)

The last is named `earliest_execution_at`, not `execution_at`, because this
system places no orders. Nothing here was ever filled. The backtest tables use
`execution_at` because there a fill genuinely happens in simulation.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy import (
    Interval as SAInterval,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.types import Availability, Engine, Freshness, MissingFactorPolicy, SignalAction
from app.models.base import Base, BigIntPk, IngestedAt


class Signal(Base):
    """One completed judgement about one instrument at one moment."""

    __tablename__ = "signal"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )

    data_asof: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    earliest_execution_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="Earliest honest fill time. Must be >= the next tradable session "
        "open after decision_at; the scorer enforces this as an invariant.",
    )

    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    action: Mapped[SignalAction] = mapped_column(
        Enum(SignalAction, name="signal_action", native_enum=False, length=20), nullable=False
    )
    policy: Mapped[MissingFactorPolicy] = mapped_column(
        Enum(MissingFactorPolicy, name="missing_factor_policy", native_enum=False, length=16),
        nullable=False,
    )
    abstained_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)

    reasons: Mapped[list[dict[str, str]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="Rendered evidence lines, produced by the engines. The UI displays "
        "these verbatim so that wording and stored arithmetic cannot drift.",
    )
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    ingested_at: Mapped[IngestedAt]

    factors: Mapped[list[SignalFactor]] = relationship(
        back_populates="signal", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_signal_instrument_decision", "instrument_id", "decision_at"),
        Index("ix_signal_decision_at", "decision_at"),
    )

    def __repr__(self) -> str:
        return f"<Signal {self.instrument_id} {self.action} {self.total_score:.1f}>"


class SignalFactor(Base):
    """One engine's contribution to one signal, with the arithmetic preserved.

    `requested_weight` versus `effective_weight` is the pair that answers "why
    was Technical's contribution unusually large today?" — the answer is
    normally that some other factor sat out.
    """

    __tablename__ = "signal_factor"

    id: Mapped[BigIntPk]
    signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("signal.id", ondelete="CASCADE"), nullable=False
    )

    engine: Mapped[Engine] = mapped_column(
        Enum(Engine, name="factor_engine", native_enum=False, length=16), nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)

    metrics: Mapped[list[dict[str, object]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="Raw and normalized values per metric, e.g. "
        '[{"name": "RSI", "raw": 61.2, "normalized": 68.0}]',
    )

    requested_weight: Mapped[float] = mapped_column(Float, nullable=False)
    effective_weight: Mapped[float] = mapped_column(Float, nullable=False)
    contribution: Mapped[float] = mapped_column(Float, nullable=False)

    availability: Mapped[Availability] = mapped_column(
        Enum(Availability, name="factor_availability", native_enum=False, length=16), nullable=False
    )
    availability_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Freshness provenance. `source_checked_at` is separate from `source_asof`
    # on purpose: for fundamentals the age of the filing is irrelevant and the
    # recency of the source check is what decides usability.
    source_asof: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    data_age: Mapped[timedelta | None] = mapped_column(SAInterval, nullable=True)
    freshness_status: Mapped[Freshness] = mapped_column(
        Enum(Freshness, name="freshness_status", native_enum=False, length=16), nullable=False
    )

    signal: Mapped[Signal] = relationship(back_populates="factors")

    __table_args__ = (Index("ix_signal_factor_signal", "signal_id"),)

    def __repr__(self) -> str:
        return f"<SignalFactor {self.engine} {self.score:.1f} -> {self.contribution:.2f}>"


class StrategyConfig(Base):
    """A versioned, complete description of a strategy.

    Weights alone are not a strategy. Changing an RSI period, switching
    normalization from percentile to z-score, moving a threshold from 70 to 75,
    or relaxing the fundamental source-check window from 3 days to 5 all produce
    different signals from identical data. They are all strategy changes, so
    they all live in one version.
    """

    __tablename__ = "strategy_config"

    id: Mapped[BigIntPk]
    version: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

    technical_params: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    normalization_method: Mapped[str] = mapped_column(
        String(24), nullable=False, default="percentile"
    )
    weights: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False, default=dict)
    signal_thresholds: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False, default=dict)

    freshness_policy: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        doc="Per-factor staleness rule. Not one max_age: technical is judged "
        "against trading sessions, news against wall-clock age, fundamentals "
        "against how recently the source was successfully checked.",
    )
    missing_factor_policy: Mapped[MissingFactorPolicy] = mapped_column(
        Enum(MissingFactorPolicy, name="strategy_missing_policy", native_enum=False, length=16),
        nullable=False,
        default=MissingFactorPolicy.ABSTAIN,
    )
    required_factors: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    sentiment_scorer: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    fundamental_revision_policy: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="as-known-then",
        doc="as-known-then takes the latest revision filed on or before asof; "
        "as-first-reported takes the earliest. Different strategies.",
    )
    universe_definition: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    execution_model: Mapped[str] = mapped_column(String(24), nullable=False, default="next-open")
    commission_model: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False, default=dict)
    slippage_model: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[IngestedAt]

    def __repr__(self) -> str:
        return f"<StrategyConfig {self.version}>"
