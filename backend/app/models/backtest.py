"""Stored backtest runs — the coordinates that make a result provable.

A number without its provenance is an anecdote. These tables exist so that
"the strategy returned 45%" can be answered with *which* strategy, over which
data, under which costs, from which commit — and so that the same run can be
executed again and checked.

**Three axes pin a run**, and all three are needed:

    strategy      kind + version + params, stored whole
    code          git_commit_sha
    data          data_snapshot_at

The strategy is stored as its canonical parts rather than as a fingerprint
alone. The digest is convenient for indexing and comparison, but 16 hex
characters cannot be read back into a strategy, and a run whose definition
survives only as a hash is not reproducible — it is merely identifiable.

**Costs are stored expanded, never as a reference to a default.** A run that
recorded "default cost model" becomes unreadable the day the default changes,
and the change is invisible: every stored run silently reinterprets itself.
The basis points that were actually applied go in the row.

**A window is a row, not a summary.** In-sample and out-of-sample results are
separate rows carrying their own sample type, and the holdout is a third kind
that can appear at most once per run. Averaging them into run-level figures
would lose exactly the comparison the walk-forward was run to make.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.types import Interval, SampleType
from app.models.base import Base, BigIntPk, IngestedAt

# Money, at the same precision the rest of the system uses.
Money = Numeric(24, 8)


class BacktestRun(Base):
    """One walk-forward experiment, with everything needed to repeat it."""

    __tablename__ = "backtest_run"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )

    # --- what was run -----------------------------------------------------
    strategy_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        doc="The constructor arguments, stored whole. A fingerprint alone "
        "identifies a strategy; only these rebuild it.",
    )
    strategy_fingerprint: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="Digest of kind+version+params, for indexing and comparison. Never "
        "the sole record of what ran.",
    )
    fitter_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc="Set when parameters were chosen per window. Null means a fixed "
        "rule, which is also what makes an in/out gap meaningless as an "
        "overfitting check.",
    )

    holdout_strategy_fingerprint: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        doc="Which strategy the holdout was finally evaluated with, written "
        "when that measurement is stored. A fitted run's final refit need not "
        "match any fold's choice, so the fit trace cannot anchor it; without "
        "this the holdout row could be rewritten to a different strategy and "
        "still replay to its own figures. Null until a holdout exists.",
    )
    fit_trace_fingerprint: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="Digest of what every window actually ran, in order. A fitted run "
        "has no single strategy, so its header is a placeholder that two "
        "fitters sharing a version produce identically however differently "
        "they behave; this is the field that tells them apart.",
    )

    # --- which code -------------------------------------------------------
    git_commit_sha: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        doc="The third axis. Two runs can share a strategy definition and a "
        "data snapshot and still differ, because the code behind the kind "
        "changed.",
    )

    git_dirty: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        doc="The working tree had uncommitted changes. The sha then does not "
        "fully describe the code that ran, and the row says so rather than "
        "looking like a clean build.",
    )

    # --- which data -------------------------------------------------------
    data_snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="Transaction-time bound. Rows ingested after this were not visible "
        "to the run and must not be visible to a replay of it.",
    )
    interval: Mapped[Interval] = mapped_column(
        Enum(Interval, name="backtest_interval", native_enum=False, length=8), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    # --- how it was executed ----------------------------------------------
    starting_cash: Mapped[Decimal] = mapped_column(Money, nullable=False)
    commission_bps: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    slippage_bps: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    min_commission: Mapped[Decimal] = mapped_column(Money, nullable=False)
    execution_model: Mapped[str] = mapped_column(String(16), nullable=False)
    bar_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- how it was split -------------------------------------------------
    train_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    eval_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    anchored: Mapped[bool] = mapped_column(Boolean, nullable=False)
    holdout_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    holdout_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    require_complete_sessions: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        doc="Whether sessions with no bar of their own were accepted. False "
        "means some days were marked at a stale price, which the window rows "
        "count individually.",
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[IngestedAt]

    windows: Mapped[list[BacktestWindow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="BacktestWindow.id"
    )

    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint(
            "(holdout_start IS NULL) = (holdout_end IS NULL)", name="holdout_both_or_neither"
        ),
        Index("ix_backtest_run_instrument", "instrument_id", "started_at"),
        Index("ix_backtest_run_strategy", "strategy_fingerprint"),
    )

    def __repr__(self) -> str:
        return (
            f"<BacktestRun {self.id} {self.strategy_kind}@{self.strategy_version} "
            f"{self.period_start}..{self.period_end}>"
        )


class BacktestWindow(Base):
    """One measurement: a period, a sample type, and what ran over it."""

    __tablename__ = "backtest_window"

    id: Mapped[BigIntPk]
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("backtest_run.id", ondelete="CASCADE"), nullable=False
    )

    window_index: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_type: Mapped[SampleType] = mapped_column(
        Enum(SampleType, name="backtest_sample_type", native_enum=False, length=16), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    # What this particular window ran. Equal to the run's definition for a
    # fixed strategy; different per window when a fitter chose them, which is
    # the only way to answer why one fold behaved as it did.
    chosen_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    chosen_version: Mapped[str] = mapped_column(String(64), nullable=False)
    chosen_params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    chosen_fingerprint: Mapped[str] = mapped_column(String(16), nullable=False)

    sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    observations: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="Daily returns behind the dispersion figures."
    )
    total_return: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    cagr: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    sharpe: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    profit_factor: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)

    trades: Mapped[int] = mapped_column(Integer, nullable=False)
    abstained: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="Sessions where the strategy declined to judge."
    )
    without_data: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="Sessions that produced no bar of their own and were marked at a "
        "stale price. Distinct from abstaining: the strategy was not asked.",
    )
    unfilled: Mapped[int] = mapped_column(Integer, nullable=False)

    ingested_at: Mapped[IngestedAt]

    run: Mapped[BacktestRun] = relationship(back_populates="windows")

    __table_args__ = (
        # A run measures each (index, sample type) once. This is also what
        # stops a holdout being scored twice and stored twice: the second
        # attempt collides rather than quietly appending a second opinion.
        UniqueConstraint("run_id", "window_index", "sample_type", name="uq_backtest_window_slot"),
        CheckConstraint("period_end >= period_start", name="window_period_ordered"),
        Index("ix_backtest_window_run", "run_id", "sample_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<BacktestWindow run={self.run_id} {self.sample_type} "
            f"#{self.window_index} {self.period_start}..{self.period_end}>"
        )
