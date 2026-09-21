"""Internal domain values.

Frozen dataclasses, not Pydantic: Pydantic earns its place at the edges (API
payloads, config, untrusted external input). Inside the domain we want cheap,
immutable, hashable values with no validation cost on every construction.

These types carry the audit trail that makes a score explainable. A `Factor`
knows its raw measurement, its normalized position, and what it actually
contributed — so "why was this 59.7?" is answerable from stored rows alone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Any


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


class SampleType(StrEnum):
    """Which side of the evidence a measurement sits on.

    Lives in `core` for the same reason `Interval` does: the pure backtest
    layers name it constantly and the persistence layer stores it, and neither
    should have to import the other to do so.
    """

    IN_SAMPLE = "IN_SAMPLE"
    """Measured over the period the rule was chosen on. Not evidence."""

    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"
    """Measured over a period the rule had not seen when it was chosen."""

    HOLDOUT = "HOLDOUT"
    """The reserved tail, evaluated once after every choice has been made.

    Window generation never produces a window over it and `walk_forward` never
    scores it. Scoring is a separate, deliberate call, because a holdout that
    is reported on every iteration gets fitted by eye — which is harder to
    notice than fitting it in code, and no less real.
    """


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


class UnknownStrategyError(Exception):
    """A definition names a kind or parameter this build cannot produce."""


# What a strategy parameter may be. Narrow on purpose: these values go into a
# JSONB column and come back out to rebuild a strategy, so anything that does
# not survive that round trip cannot be allowed in. A nested structure would
# also need its own deep-freeze and canonical ordering, and no strategy needs
# one.
_ALLOWED_PARAMS = (str, int, float, bool, type(None))


def _canonical_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """A JSON-safe copy with stable ordering.

    Copying is the point. `frozen=True` freezes the dataclass's own fields,
    not the dict one of them points at, so a caller holding the original could
    change a definition after it had already been run:

        params = {"short": 10, "long": 30}
        definition = StrategyDefinition(..., params=params)
        ...                                   # runs 10/30
        params["short"] = 20                  # now builds 20/30

    Same object, same version, different behaviour — which is the very thing
    the definition exists to make impossible.
    """
    out: dict[str, Any] = {}
    for key in sorted(params):
        value = params[key]
        if isinstance(value, Enum):
            # A StrEnum survives JSON as its value and rebuilds from it.
            value = value.value
        if not isinstance(value, _ALLOWED_PARAMS):
            raise UnknownStrategyError(
                f"parameter {key!r} is {type(value).__name__}; a definition must "
                "survive being stored and read back, so parameters are limited to "
                "strings, numbers, booleans and null"
            )
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise UnknownStrategyError(f"parameter {key!r} is {value}, which JSON cannot hold")
        out[key] = value
    return out


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    """Everything needed to rebuild a strategy, and nothing else.

    This is what a `backtest_run` row holds, which is why it lives in `core`
    rather than beside the strategies: the persistence layer has to name a
    stored strategy without reaching up into the engines that run it.
    Constructing the running object is `app.backtest.strategies.build`, and
    that function is the only way — so the definition that was stored, the one
    that will be replayed and the one that actually ran are the same by
    construction rather than by discipline.

    `version` must change whenever behaviour does. 20/60 and 10/30 produce
    different trades from identical data, so they are different strategies;
    the params make that visible even when someone forgets, since they are
    stored too and compared on replay.
    """

    kind: str
    version: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise UnknownStrategyError("a strategy definition needs a kind")
        if not self.version.strip():
            raise UnknownStrategyError("a strategy definition needs a version")
        object.__setattr__(self, "params", MappingProxyType(_canonical_params(self.params)))

    @property
    def canonical(self) -> str:
        """The stored form, byte-for-byte stable.

        Ordering is fixed, so the same definition produces the same string on
        any machine and in any process. Two runs can then be compared by what
        they ran rather than by what they were called.
        """
        return json.dumps(
            {"kind": self.kind, "version": self.version, "params": dict(self.params)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @property
    def fingerprint(self) -> str:
        """A short digest of `canonical`, for indexing and comparison.

        Strategy identity is kind + version + params, never version alone. A
        version is written by a person and nothing stops two definitions
        sharing one while behaving differently; the params are what actually
        determine behaviour, so they are part of the identity. `git_commit_sha`
        covers the third axis — a change to the code behind the kind.
        """
        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> str:
        """One line for a report or a log."""
        if not self.params:
            return f"{self.kind}@{self.version}"
        inner = " ".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.kind}@{self.version} ({inner})"
