"""Internal domain values.

Frozen dataclasses, not Pydantic: Pydantic earns its place at the edges (API
payloads, config, untrusted external input). Inside the domain we want cheap,
immutable, hashable values with no validation cost on every construction.

These types carry the audit trail that makes a score explainable. A `Factor`
knows its raw measurement, its normalized position, and what it actually
contributed — so "why was this 59.7?" is answerable from stored rows alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class Engine(StrEnum):
    """The four scoring engines."""

    TECHNICAL = "TECHNICAL"
    FUNDAMENTAL = "FUNDAMENTAL"
    SENTIMENT = "SENTIMENT"
    PORTFOLIO = "PORTFOLIO"


class Freshness(StrEnum):
    """How current the data behind a factor is.

    Judged differently per engine — see `app.scoring.availability`. Technical
    data is judged against trading sessions, news against wall-clock age, and
    fundamentals against when we last successfully checked the source (a
    quarterly filing is old by nature; that is not staleness).
    """

    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"


class Availability(StrEnum):
    """Whether a factor may be used in scoring at a given moment."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class MissingFactorPolicy(StrEnum):
    """What to do when a required factor is unavailable.

    ABSTAIN is the default. It means the strategy declines to judge at this
    moment — no signal, no new entry — while time and existing positions carry
    on. That is different from deleting the period from the backtest, which
    would itself bias the result.

    ZERO keeps scoring with the factor contributing nothing, which shrinks the
    total's scale; thresholds must be scaled to match or signals stop firing
    entirely. RENORMALIZE redistributes weight to the survivors, which silently
    turns it into a different strategy, so it is opt-in only.
    """

    ABSTAIN = "ABSTAIN"
    ZERO = "ZERO"
    RENORMALIZE = "RENORMALIZE"


class SignalAction(StrEnum):
    """What the signal suggests looking at. Never an order instruction."""

    BUY_INTEREST = "BUY_INTEREST"
    WATCH = "WATCH"
    CAUTION = "CAUTION"
    ABSTAINED = "ABSTAINED"


class ReasonStatus(StrEnum):
    """Display state of a single piece of evidence: ✓ / △ / ✗."""

    SUPPORTS = "SUPPORTS"
    NEUTRAL = "NEUTRAL"
    OPPOSES = "OPPOSES"


class Interval(StrEnum):
    """Bar sizes. Toss publishes 1-minute and daily; longer bars are derived.

    Lives in `core` because a bar size is a property of the data, not of how
    it is stored: the pure layers — the backtest engine above all — name
    intervals constantly and must not import the ORM to do it. `app.models`
    re-exports it, so the persistence layer's vocabulary is unchanged.
    """

    MIN_1 = "1m"
    DAY_1 = "1d"


@dataclass(frozen=True, slots=True)
class Bar:
    """One completed OHLCV bar, detached from any ORM.

    Plain values so a simulation cannot hold a live ORM object whose lazy
    loads would reach the database outside the point-in-time filter, and so
    the pure layers can name the type without importing the repository that
    produces it.

    `ts` is the bar's start; `available_at` is when it completed and therefore
    when its close became knowable. Keeping both is what lets a reader answer
    "has this bar finished" without guessing from the clock.
    """

    ts: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class Metric:
    """One raw measurement plus where it sits in the cross-section.

    `raw` is the measured value in its natural unit (RSI 61.2, ROE 0.142).
    `normalized` is 0-100, a percentile within the same-moment universe, which
    is what makes an RSI-based 80 comparable to an ROE-based 80.
    """

    name: str
    raw: float
    normalized: float
    detail: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.normalized <= 100.0:
            raise ValueError(
                f"normalized must be 0-100 for metric {self.name!r}; got {self.normalized}"
            )


@dataclass(frozen=True, slots=True)
class DataProvenance:
    """Where a factor's inputs came from and how current they are.

    `source_asof` is the timestamp of the newest underlying datum.
    `source_checked_at` is when we last successfully reached the source. For
    fundamentals the second matters and the first does not: a filing 36 days old
    with a source checked 2 hours ago is healthy; the same filing with a source
    last reached 10 days ago means we may be missing a new one.
    """

    source_asof: datetime | None
    source_checked_at: datetime | None
    data_age: timedelta | None
    freshness: Freshness


@dataclass(frozen=True, slots=True)
class Factor:
    """One engine's verdict, with the full arithmetic kept intact.

    `requested_weight` is what the strategy config asked for; `effective_weight`
    is what was actually applied after availability was resolved. Keeping both
    is the point — it answers "why was Technical's contribution unusually large
    today?" without re-running anything.
    """

    engine: Engine
    score: float
    metrics: tuple[Metric, ...]
    requested_weight: float
    effective_weight: float
    availability: Availability
    provenance: DataProvenance
    availability_reason: str | None = None

    @property
    def contribution(self) -> float:
        """What this factor actually added to the total."""
        return self.score * self.effective_weight

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 100.0:
            raise ValueError(f"{self.engine} score must be 0-100; got {self.score}")
        for name, w in (
            ("requested_weight", self.requested_weight),
            ("effective_weight", self.effective_weight),
        ):
            if not 0.0 <= w <= 1.0:
                raise ValueError(f"{self.engine} {name} must be 0-1; got {w}")
        if self.availability is Availability.UNAVAILABLE and self.availability_reason is None:
            raise ValueError(f"{self.engine} is UNAVAILABLE but carries no reason")


@dataclass(frozen=True, slots=True)
class SignalReason:
    """One rendered line of evidence shown in the UI.

    Produced by the engine that computed it, never composed in the frontend —
    the displayed wording and the stored arithmetic must not drift apart.
    """

    status: ReasonStatus
    text: str
    engine: Engine
    metric_name: str | None = None


@dataclass(frozen=True, slots=True)
class ScoredSignal:
    """A completed judgement, with its three separate clocks.

    `data_asof`       the data this was computed from (e.g. the 09/19 close)
    `decision_at`     when the judgement was finalised (after that close)
    `earliest_execution_at`
                      the soonest an order could honestly fill (09/22 open)

    Named `earliest_execution_at` rather than `execution_at` because this system
    places no orders; nothing here was ever filled.
    """

    instrument_id: int
    data_asof: datetime
    decision_at: datetime
    earliest_execution_at: datetime
    total_score: float
    action: SignalAction
    factors: tuple[Factor, ...]
    reasons: tuple[SignalReason, ...]
    strategy_version: str
    policy: MissingFactorPolicy
    abstained_reason: str | None = None

    @property
    def effective_weight_total(self) -> float:
        """Sum of applied weights. Below 1.0 means some factor sat out."""
        return sum(f.effective_weight for f in self.factors)
