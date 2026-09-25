"""The review gates of the forward record, and the rule for letting the overlay act — fixed in advance.

Pure. Written before the record has a single measured outcome, on purpose:
a rule chosen after looking at the results is fitted to them. The thresholds
here are committed first, so the history shows they came first.

**Korean entry days.** The overlay reads Korean news only, so the record it
is judged on is the Korean one, and a day is a Korean session. Twenty names
judged on one day share that day's market; they are one observation of it,
not twenty. Every statistic averages within a day first and then across
days, and counts days.

**Gates**, on the 5-session horizon (the overlay's half-lives are one to ten
days, so this is its horizon):

- About a month: 20 entry days. Read the record, decide nothing.
- About three months: 60 entry days. The overlay decision is taken on the
  first 60 entry days — a fixed sample, decided once. Looking again every day
  after would be asking the same question until chance answers yes.
- Only if the first 60 days held too little news (fewer than 20 days of
  either kind): the sample is extended once, to the first 120 entry days.
  If that is still too little, the answer is no, and it is final.

**The overlay may be proposed to change actions only if all of these hold**
on the decision sample's 5-session excess returns:

1. Enough of both kinds of news: at least 20 days with a "good news" signal
   and 20 with a "bad news" one.
2. Good news beat bad news: the difference of the two day-averaged means is
   positive with a t of at least 2. The two sides share days, and their
   standard errors are combined as if they did not; that ignores a
   correlation rather than inventing one, and is stated here so it is known.
3. It was positive in both halves of the sample, split at the median entry
   day, each half with at least 5 days of each kind. A half too thin to test
   fails: a result from one half is that half's result.
4. It was positive in RISK_ON and in the other known regimes (UNKNOWN is left
   out), where each side has at least 5 days of each; a side without that
   many is reported as untested and does not count against.

Passing is not switching it on. It licenses a proposal: a new strategy
version, its action distribution counted before any threshold is accepted,
and evaluation without touching the holdout.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

OVERLAY_HORIZON = 5


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    horizon: int
    days: int


GATES = (
    Gate("one month", OVERLAY_HORIZON, 20),
    Gate("three months", OVERLAY_HORIZON, 60),
    Gate("six months, only if news was thin", OVERLAY_HORIZON, 120),
)
DECISION_GATE = GATES[1]
EXTENDED_GATE = GATES[2]
MIN_DAYS_PER_SIDE = 20
MIN_T = 2.0
MIN_DAYS_PER_SPLIT = 5


@dataclass(frozen=True, slots=True)
class DayStats:
    """A mean over days, each day the mean of its own observations."""

    days: int
    mean: float | None
    se: float | None

    @property
    def t(self) -> float | None:
        if self.mean is None or not self.se:
            return None
        return self.mean / self.se


def by_day(observations: Sequence[tuple[datetime, float]]) -> DayStats:
    daily: dict[datetime, list[float]] = defaultdict(list)
    for day, value in observations:
        daily[day].append(value)
    means = [statistics.fmean(v) for v in daily.values()]
    if not means:
        return DayStats(0, None, None)
    if len(means) < 2:
        return DayStats(1, means[0], None)
    return DayStats(
        len(means), statistics.fmean(means), statistics.stdev(means) / math.sqrt(len(means))
    )


@dataclass(frozen=True, slots=True)
class Spread:
    good: DayStats
    bad: DayStats

    @property
    def difference(self) -> float | None:
        if self.good.mean is None or self.bad.mean is None:
            return None
        return self.good.mean - self.bad.mean

    @property
    def t(self) -> float | None:
        if self.difference is None or self.good.se is None or self.bad.se is None:
            return None
        se = math.sqrt(self.good.se**2 + self.bad.se**2)
        return self.difference / se if se else None


def spread(good: Sequence[tuple[datetime, float]], bad: Sequence[tuple[datetime, float]]) -> Spread:
    return Spread(by_day(good), by_day(bad))


@dataclass
class Verdict:
    ready: bool
    passed: bool
    reasons: list[str] = field(default_factory=list)


def _split_ok(name: str, part: Spread, reasons: list[str]) -> bool | None:
    """True or False when the part has enough days on both sides, None when untested."""
    if part.good.days < MIN_DAYS_PER_SPLIT or part.bad.days < MIN_DAYS_PER_SPLIT:
        reasons.append(f"{name}: untested ({part.good.days} good, {part.bad.days} bad days)")
        return None
    held = part.difference is not None and part.difference > 0
    reasons.append(
        f"{name}: difference {part.difference:+.2f} — {'held' if held else 'did not hold'}"
    )
    return held


def news_days_ok(whole: Spread) -> bool:
    return whole.good.days >= MIN_DAYS_PER_SIDE and whole.bad.days >= MIN_DAYS_PER_SIDE


def decision_sample(entry_days: int, enough_news_in: dict[int, bool]) -> tuple[int | None, bool]:
    """How many of the first entry days the decision is taken on, and whether that is final.

    `enough_news_in` says, for each candidate sample size already reached,
    whether it holds enough news days. None when no decision is due yet.
    """
    if entry_days < DECISION_GATE.days:
        return None, False
    if enough_news_in.get(DECISION_GATE.days, False):
        return DECISION_GATE.days, True
    if entry_days < EXTENDED_GATE.days:
        return None, False
    return EXTENDED_GATE.days, True


def overlay_verdict(
    *,
    sample_days: int | None,
    entry_days: int,
    whole: Spread,
    halves: tuple[Spread, Spread],
    risk_on: Spread,
    other_regimes: Spread,
) -> Verdict:
    """The pre-registered decision on whether the overlay may be proposed to act.

    `whole` and the splits are over the first `sample_days` entry days only.
    """
    reasons: list[str] = []
    if sample_days is None:
        if entry_days < DECISION_GATE.days:
            reasons.append(f"{entry_days} of {DECISION_GATE.days} entry days measured; not yet")
        else:
            reasons.append(
                f"the first {DECISION_GATE.days} days held too little news; waiting for "
                f"{EXTENDED_GATE.days} ({entry_days} so far)"
            )
        return Verdict(ready=False, passed=False, reasons=reasons)

    reasons.append(f"decided on the first {sample_days} entry days")
    if not news_days_ok(whole):
        reasons.append(
            f"too little news even in {sample_days} days: {whole.good.days} good, "
            f"{whole.bad.days} bad (each needs {MIN_DAYS_PER_SIDE}); the answer is no"
        )
        return Verdict(ready=True, passed=False, reasons=reasons)

    ok = True
    t = whole.t
    if whole.difference is None or t is None or whole.difference <= 0 or t < MIN_T:
        ok = False
    shown_t = "-" if t is None else f"{t:.2f}"
    reasons.append(
        f"good minus bad: {whole.difference:+.2f} points, t {shown_t} (needs > 0 and t >= {MIN_T})"
    )
    for name, part in (("first half", halves[0]), ("second half", halves[1])):
        if _split_ok(name, part, reasons) is not True:
            ok = False
    for name, part in (("RISK_ON", risk_on), ("other regimes", other_regimes)):
        if _split_ok(name, part, reasons) is False:
            ok = False
    return Verdict(ready=True, passed=ok, reasons=reasons)


# --- half-life -----------------------------------------------------------

AGE_BUCKETS = (
    (0.0, 1.0, "under 1 day"),
    (1.0, 3.0, "1-3 days"),
    (3.0, 7.0, "3-7 days"),
    (7.0, math.inf, "7+ days"),
)


def age_bucket(age_days: float) -> str:
    for low, high, label in AGE_BUCKETS:
        if low <= age_days < high:
            return label
    return AGE_BUCKETS[0][2]


def by_age(observations: Sequence[tuple[float, datetime, float]]) -> dict[str, DayStats]:
    """Return in the event's own direction, by how old the event was when judged.

    `observations` are (age in days, entry day, excess times the sign of the
    event's sentiment). If news moves prices and then fades, this falls with
    age; where it has halved is what a half-life should say. Exploratory: it
    informs the next parameter version, it does not set one.
    """
    grouped: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for age, day, value in observations:
        if age < 0:
            # An event after the judgement was not part of it.
            continue
        grouped[age_bucket(age)].append((day, value))
    return {label: by_day(grouped[label]) for _, _, label in AGE_BUCKETS if label in grouped}
