"""ORM models.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate reflects against. A model that is not imported here is
invisible to migrations.
"""

from app.core.types import Interval, SampleType
from app.models.backtest import BacktestRun, BacktestWindow
from app.models.base import Base
from app.models.collector import CollectorRun, CollectorStatus
from app.models.filing import Filing
from app.models.fundamental import (
    FiscalPeriod,
    Fundamental,
    FundamentalSource,
)
from app.models.instrument import Instrument, SymbolHistory
from app.models.market import (
    Candle,
    CorporateAction,
    CorporateActionType,
    FxRate,
)
from app.models.portfolio import (
    Holding,
    PortfolioSnapshot,
    Transaction,
    TransactionSide,
)
from app.models.signal import Signal, SignalFactor, StrategyConfig

__all__ = [
    "BacktestRun",
    "BacktestWindow",
    "Base",
    "Candle",
    "CollectorRun",
    "CollectorStatus",
    "CorporateAction",
    "CorporateActionType",
    "Filing",
    "FiscalPeriod",
    "Fundamental",
    "FundamentalSource",
    "FxRate",
    "Holding",
    "Instrument",
    "Interval",
    "PortfolioSnapshot",
    "SampleType",
    "Signal",
    "SignalFactor",
    "StrategyConfig",
    "SymbolHistory",
    "Transaction",
    "TransactionSide",
]
