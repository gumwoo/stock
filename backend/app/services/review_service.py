"""The forward record read against its review gates: where it stands, and what it licenses.

Reads only. The rules are in `app/scoring/review.py`, fixed before the
record had data; this gathers the record into their shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.calendar import Market, MarketCalendar
from app.core.clock import ensure_utc, utc_now
from app.scoring.regime import Label
from app.scoring.review import (
    DECISION_GATE,
    EXTENDED_GATE,
    GATES,
    OVERLAY_HORIZON,
    DayStats,
    Gate,
    Spread,
    Verdict,
    by_age,
    decision_sample,
    news_days_ok,
    overlay_verdict,
    spread,
)
from app.services.forward_service import Record, overlay_bucket, signal_records


@dataclass(frozen=True, slots=True)
class GateStatus:
    gate: Gate
    days: int
    reached: bool
    # Earliest the gate can be reached if a signal is recorded every session
    # from now on. An estimate, not a promise: a day without a signal is a
    # day that does not count.
    earliest: datetime | None


@dataclass
class Review:
    first_entry: datetime | None
    gates: list[GateStatus] = field(default_factory=list)
    spread: Spread | None = None
    verdict: Verdict | None = None
    half_life: dict[str, DayStats] = field(default_factory=dict)


def _days(records: list[Record], horizon: int) -> int:
    return len({r.entry_at for r in records if r.horizon == horizon})


def _earliest(have: int, need: int, horizon: int, now: datetime) -> datetime | None:
    """The close at which `need` entry days could have their outcome, from `have` today."""
    calendar = MarketCalendar(Market.KR)
    today = calendar.local_today(now)
    if have >= need or today >= calendar.last_session:
        return None
    horizon_end = min(today + timedelta(days=400), calendar.last_session)
    sessions = calendar.sessions_between(today, horizon_end)
    # Each missing entry day enters at a future open and is measured at the
    # close of its horizon-th session.
    index = (need - have) + horizon - 1
    return calendar.session_close(sessions[min(index, len(sessions) - 1)])


def _pairs(records: list[Record], label: str) -> list[tuple[datetime, float]]:
    return [(r.entry_at, r.excess) for r in records if overlay_bucket(r.overlay_points) == label]


def _spread(records: list[Record]) -> Spread:
    return spread(_pairs(records, "good news"), _pairs(records, "bad news"))


def _half_life(records: list[Record]) -> dict[str, DayStats]:
    observations: list[tuple[float, datetime, float]] = []
    for r in records:
        for cluster in r.overlay_detail or []:
            sentiment = cluster.get("sentiment")
            first = cluster.get("first_at")
            if (
                not isinstance(sentiment, int | float)
                or sentiment == 0
                or not isinstance(first, str)
            ):
                continue
            age = r.decision_at - ensure_utc(datetime.fromisoformat(first), field="first_at")
            sign = 1.0 if sentiment > 0 else -1.0
            observations.append((age.total_seconds() / 86400, r.entry_at, r.excess * sign))
    return by_age(observations)


def review(session: Session, *, now: datetime | None = None) -> Review:
    now = now or utc_now()
    records, _ = signal_records(session)
    # The overlay reads Korean news: the record it answers to is the Korean one.
    korean = [r for r in records if r.market is Market.KR]
    out = Review(first_entry=min((r.entry_at for r in korean), default=None))
    for gate in GATES:
        days = _days(korean, gate.horizon)
        out.gates.append(
            GateStatus(gate, days, days >= gate.days, _earliest(days, gate.days, gate.horizon, now))
        )

    five = [r for r in korean if r.horizon == OVERLAY_HORIZON]
    entry_days = sorted({r.entry_at for r in five})

    def first(n: int) -> list[Record]:
        cutoff = set(entry_days[:n])
        return [r for r in five if r.entry_at in cutoff]

    enough = {
        n: news_days_ok(_spread(first(n)))
        for n in (DECISION_GATE.days, EXTENDED_GATE.days)
        if len(entry_days) >= n
    }
    sample_days, _ = decision_sample(len(entry_days), enough)
    sample = first(sample_days) if sample_days is not None else five
    out.spread = _spread(sample)
    sample_entries = sorted({r.entry_at for r in sample})
    median = sample_entries[len(sample_entries) // 2] if sample_entries else None
    out.verdict = overlay_verdict(
        sample_days=sample_days,
        entry_days=len(entry_days),
        whole=out.spread,
        halves=(
            _spread([r for r in sample if median is not None and r.entry_at < median]),
            _spread([r for r in sample if median is not None and r.entry_at >= median]),
        ),
        risk_on=_spread([r for r in sample if r.regime == Label.RISK_ON]),
        other_regimes=_spread(
            [r for r in sample if r.regime not in (None, Label.RISK_ON, Label.UNKNOWN)]
        ),
    )
    out.half_life = _half_life(five)
    return out
