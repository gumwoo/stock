"""ORM models.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate reflects against. A model that is not imported here is
invisible to migrations.
"""

from app.core.types import Interval, SampleType
from app.models.attention import SearchTrend, SignalAttention
from app.models.backtest import BacktestRun, BacktestWindow
from app.models.base import Base
from app.models.collector import CollectorRun, CollectorStatus
from app.models.disclosure import Disclosure
from app.models.filing import Filing
from app.models.forward import CandidateOutcome, CandidateSnapshot, SignalOutcome
from app.models.fundamental import (
    FiscalPeriod,
    Fundamental,
    FundamentalSource,
)
from app.models.instrument import Instrument, Listing, SymbolHistory
from app.models.intraday import IndexMinuteBar, IntradaySummary, MinuteBar, MinuteFetch
from app.models.kis import KisCredential
from app.models.llm import LlmCall
from app.models.market import (
    Candle,
    CorporateAction,
    CorporateActionType,
    FxRate,
    MarketIndexBar,
)
from app.models.news import (
    Decider,
    HitDecision,
    MatchMethod,
    NewsItem,
    NewsMention,
    NewsQueryHit,
    NewsRelevanceDecision,
    NewsSentiment,
    NewsSource,
    NewsSweepCoverage,
    RuleAudit,
    SentimentEvent,
)
from app.models.portfolio import (
    Holding,
    PortfolioSnapshot,
    Transaction,
    TransactionSide,
)
from app.models.promotion import InstrumentPromotion
from app.models.quota import ApiCallBucket
from app.models.signal import (
    Signal,
    SignalFactor,
    SignalOverlay,
    SignalRegime,
    StrategyConfig,
)
from app.models.watchlist import WatchlistMember, WatchlistSnapshot

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
    "Disclosure",
    "Filing",
    "FiscalPeriod",
    "Fundamental",
    "FundamentalSource",
    "FxRate",
    "HitDecision",
    "Holding",
    "IndexMinuteBar",
    "Instrument",
    "InstrumentPromotion",
    "Interval",
    "IntradaySummary",
    "KisCredential",
    "Listing",
    "LlmCall",
    "MarketIndexBar",
    "MatchMethod",
    "MinuteBar",
    "MinuteFetch",
    "NewsItem",
    "NewsMention",
    "NewsQueryHit",
    "NewsRelevanceDecision",
    "NewsSentiment",
    "NewsSource",
    "NewsSweepCoverage",
    "PortfolioSnapshot",
    "RuleAudit",
    "SampleType",
    "SearchTrend",
    "SentimentEvent",
    "Signal",
    "SignalAttention",
    "SignalFactor",
    "SignalOutcome",
    "SignalOverlay",
    "SignalRegime",
    "StrategyConfig",
    "SymbolHistory",
    "Transaction",
    "TransactionSide",
    "WatchlistMember",
    "WatchlistSnapshot",
]
