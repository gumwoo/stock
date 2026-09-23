"""News events as an overlay on the score, not a factor in it. Pure arithmetic.

The base score — technical 0.6, fundamental 0.4 — has ten years of history
behind it and was validated against that history. News has none: collection
began the day it was switched on. So news does not enter the weights. It is
read into events, the events decay, and the result sits beside the score as a
bounded number of points, recorded with every signal. It does not change the
action. Whether it should is a question only forward testing can answer
(Phase 4-8), because there is no past to test it on.

**One event, however many outlets.** A buyback announcement is reported by
twenty outlets within hours; summing twenty readings would make it twenty
events. Readings of one company are grouped into a cluster when they share an
event type and the later one appeared within `cluster_window` of the first.
The cluster counts once: its direction is the confidence-weighted mean of its
readings, its intensity and confidence the largest among them, and its age
runs from the first article. Two genuinely different events of one type on the
same day merge; that errs towards counting less, which is the safe side.

**Events fade.** Each cluster's weight halves every `half_life` of its event
type. The half-lives are priors, not measurements: a price move is yesterday's
news within a day, an acquisition matters for weeks. Forward testing is what
should replace them, and the parameters carry a version so readings made under
one set are not confused with another.

**Bounded.** The sum of contributions passes through tanh and is scaled to
`max_points`, so no pile of news can move the displayed score by more than
that, and a single strong event already moves it most of the way.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.clock import ensure_utc

# Hours for an event's weight to halve. Priors — see the module docstring.
DEFAULT_HALF_LIVES: Mapping[str, float] = {
    "PRICE_MOVE": 24,
    "OTHER": 24,
    "INDUSTRY": 48,
    "PRODUCT": 72,
    "MANAGEMENT": 72,
    "ANALYST_RATING": 72,
    "EARNINGS": 120,
    "GUIDANCE": 120,
    "SHAREHOLDER_RETURN": 120,
    "ORDER_CONTRACT": 168,
    "CAPITAL_RAISE": 168,
    "LEGAL_REGULATORY": 168,
    "MERGER_ACQUISITION": 240,
}


@dataclass(frozen=True, slots=True)
class OverlayParams:
    version: int = 1
    half_lives: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_HALF_LIVES))
    cluster_window: timedelta = timedelta(hours=24)
    max_points: float = 10.0
    # Readings the model itself was unsure of do not count.
    min_confidence: float = 0.3
    # Events older than this many half-lives weigh under 2% and are dropped.
    horizon_half_lives: float = 6.0

    def half_life(self, event_type: str) -> timedelta:
        return timedelta(hours=self.half_lives.get(event_type, self.half_lives["OTHER"]))

    def lookback(self) -> timedelta:
        """How far back any event can still count."""
        return timedelta(hours=max(self.half_lives.values()) * self.horizon_half_lives)


@dataclass(frozen=True, slots=True)
class EventReading:
    news_item_id: int
    available_at: datetime
    event_type: str
    sentiment: float
    intensity: float
    confidence: float
    title: str = ""


@dataclass(frozen=True, slots=True)
class Cluster:
    event_type: str
    first_at: datetime
    articles: int
    sentiment: float
    intensity: float
    confidence: float
    decay: float
    contribution: float
    title: str
    news_item_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Overlay:
    points: float
    raw: float
    clusters: tuple[Cluster, ...]
    readings_used: int
    readings_dropped: int


def cluster(readings: Sequence[EventReading], window: timedelta) -> list[list[EventReading]]:
    """Group readings that report one event: same type, close in time.

    Joined to the open cluster of the same type when within `window` of that
    cluster's first article, measured from the first rather than the last so
    a steady trickle of one type does not chain into a single week-long event.
    """
    groups: list[list[EventReading]] = []
    open_by_type: dict[str, list[EventReading]] = {}
    for reading in sorted(readings, key=lambda r: (r.available_at, r.news_item_id)):
        current = open_by_type.get(reading.event_type)
        if current is not None and reading.available_at - current[0].available_at <= window:
            current.append(reading)
            continue
        fresh = [reading]
        groups.append(fresh)
        open_by_type[reading.event_type] = fresh
    return groups


def compute_overlay(
    readings: Sequence[EventReading], *, asof: datetime, params: OverlayParams
) -> Overlay:
    """The overlay at `asof` from readings the caller has already bounded to it."""
    asof = ensure_utc(asof, field="asof")
    usable = [
        r
        for r in readings
        if r.confidence >= params.min_confidence
        and ensure_utc(r.available_at, field="available_at") <= asof
        and asof - ensure_utc(r.available_at, field="available_at") <= params.lookback()
    ]
    clusters: list[Cluster] = []
    for group in cluster(usable, params.cluster_window):
        first = group[0]
        age = asof - ensure_utc(first.available_at, field="available_at")
        half_life = params.half_life(first.event_type)
        if age > half_life * params.horizon_half_lives:
            continue
        decay = 0.5 ** (age / half_life)
        weight = sum(r.confidence for r in group)
        sentiment = sum(r.sentiment * r.confidence for r in group) / weight
        intensity = max(r.intensity for r in group)
        confidence = max(r.confidence for r in group)
        clusters.append(
            Cluster(
                event_type=first.event_type,
                first_at=first.available_at,
                articles=len(group),
                sentiment=sentiment,
                intensity=intensity,
                confidence=confidence,
                decay=decay,
                contribution=sentiment * intensity * confidence * decay,
                title=first.title,
                news_item_ids=tuple(r.news_item_id for r in group),
            )
        )
    raw = sum(c.contribution for c in clusters)
    clusters.sort(key=lambda c: -abs(c.contribution))
    return Overlay(
        points=params.max_points * math.tanh(raw),
        raw=raw,
        clusters=tuple(clusters),
        readings_used=sum(c.articles for c in clusters),
        readings_dropped=len(readings) - sum(c.articles for c in clusters),
    )
