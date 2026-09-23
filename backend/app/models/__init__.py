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
from app.models.forward import CandidateOutcome, CandidateSnapshot, SignalOutcome
from app.models.fundamental import (
    FiscalPeriod,
    Fundamental,
    FundamentalSource,
)
from app.models.instrument import Instrument, Listing, SymbolHistory
from app.models.llm import LlmCall
from app.models.market import (
    Candle,
    CorporateAction,
    CorporateActionType,
    FxRate,
)
from app.models.news import (
    Decider,
    HitDecision,
    MatchMethod,
    NewsItem,
    NewsMention,
    NewsQueryHit,
    NewsRelevanceDecision,
    NewsSource,
    NewsSweepCoverage,
)
from app.models.portfolio import (
    Holding,
    PortfolioSnapshot,
    Transaction,
    TransactionSide,
)
from app.models.promotion import InstrumentPromotion
from app.models.quota import ApiCallBucket
from app.models.signal import Signal, SignalFactor, SignalOverlay, StrategyConfig

__all__ = [
    "ApiCallBucket",
    "BacktestRun",
    "BacktestWindow",
    "Base",
    "CandidateOutcome",
    "CandidateSnapshot",
    "Candle",
    "CollectorRun",
    "CollectorStatus",
    "CorporateAction",
    "CorporateActionType",
    "Decider",
    "Filing",
    "FiscalPeriod",
    "Fundamental",
    "FundamentalSource",
    "FxRate",
    "HitDecision",
    "Holding",
    "Instrument",
    "InstrumentPromotion",
    "Interval",
    "Listing",
    "LlmCall",
    "MatchMethod",
    "NewsItem",
    "NewsMention",
    "NewsQueryHit",
    "NewsRelevanceDecision",
    "NewsSentiment",
    "NewsSource",
    "NewsSweepCoverage",
    "PortfolioSnapshot",
    "SampleType",
    "SentimentEvent",
    "Signal",
    "SignalFactor",
    "SignalOutcome",
    "SignalOverlay",
    "StrategyConfig",
    "SymbolHistory",
    "Transaction",
    "TransactionSide",
]
