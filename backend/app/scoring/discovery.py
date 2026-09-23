"""Which untracked names are suddenly being written about. Pure arithmetic.

The question is a surge, not a level. A large company is always in the news,
and ranking by count alone would put the same thirty names on top every day.
What deserves a look is a name mentioned far more in the recent window than
its own past says it usually is.

**Counts are per day read, not per day.** A sweep reads one page of a hundred
results, and for a busy name that page covers a few hours, not the stretch
since the last sweep. Counting mentions over a nominal window then compares a
full day of one name with four hours of another, and the first ranking run
made that concrete: every name at the top was a large cap with a hundred
articles in the recent window and none before it, because nothing before it
had been read. So each count comes with the time actually covered, and rates
are compared, never raw counts.

**The ratio is smoothed**, one pseudo-mention on each side. Without it a name
with one article today and none before has an infinite surge; with it, one
against an expected nothing scores 2, and ten against an expected one 5.5.

**A name whose history was barely read is not ranked.** It is counted as
unmeasured instead, so the list does not quietly favour what we know least
about.

This is a list of names worth looking at, not a signal. Picking stocks by
today's coverage and then backtesting them imports selection bias; see the
README's limits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

# Pseudo-mentions added to both sides of the ratio.
SMOOTHING = 1.0
_DAY = 86400.0


@dataclass(frozen=True, slots=True)
class MentionCounts:
    instrument_id: int
    recent: int
    baseline: int
    recent_days: float
    """How much of the recent window a sweep actually read, in days."""
    baseline_days: float
    """How much of the baseline window a sweep actually read, in days."""


@dataclass(frozen=True, slots=True)
class Surge:
    instrument_id: int
    recent: int
    baseline: int
    recent_days: float
    baseline_days: float
    expected: float
    score: float


def covered_days(
    intervals: Sequence[tuple[datetime, datetime]], start: datetime, end: datetime
) -> float:
    """Length of the union of `intervals` inside `[start, end]`, in days.

    Sweeps overlap by design — the watermark reaches back past the previous
    run — so summing lengths would count the overlap twice.
    """
    clipped = sorted(
        (max(lo, start), min(hi, end)) for lo, hi in intervals if hi > start and lo < end
    )
    total = 0.0
    current_lo: datetime | None = None
    current_hi: datetime | None = None
    for lo, hi in clipped:
        if current_hi is None or lo > current_hi:
            if current_lo is not None and current_hi is not None:
                total += (current_hi - current_lo).total_seconds()
            current_lo, current_hi = lo, hi
        elif hi > current_hi:
            current_hi = hi
    if current_lo is not None and current_hi is not None:
        total += (current_hi - current_lo).total_seconds()
    return total / _DAY


def inside(moment: datetime, intervals: Sequence[tuple[datetime, datetime]]) -> bool:
    """Whether a sweep that read `intervals` could have seen something at `moment`."""
    return any(lo <= moment <= hi for lo, hi in intervals)


def surge(counts: MentionCounts) -> Surge:
    """Recent mentions against what the baseline rate predicts for the time read."""
    if counts.recent_days <= 0 or counts.baseline_days <= 0:
        raise ValueError("a surge needs both windows to have been read")
    rate = counts.baseline / counts.baseline_days
    expected = rate * counts.recent_days
    return Surge(
        instrument_id=counts.instrument_id,
        recent=counts.recent,
        baseline=counts.baseline,
        recent_days=counts.recent_days,
        baseline_days=counts.baseline_days,
        expected=expected,
        score=(counts.recent + SMOOTHING) / (expected + SMOOTHING),
    )


def rank(
    counts: Sequence[MentionCounts],
    *,
    min_recent: int,
    min_recent_days: float,
    min_baseline_days: float,
    top: int,
) -> tuple[list[Surge], int]:
    """The `top` surges, and how many names had too little read to be ranked.

    Coverage is checked first, so a name whose mentions fell outside anything
    a sweep recorded reading is counted as unmeasured rather than as quiet.
    Then `min_recent` keeps one stray article from outranking a real story:
    two mentions against an expected nothing score 3, which beats a name going
    from twenty to forty.
    """
    ranked: list[Surge] = []
    unmeasured = 0
    for c in counts:
        if c.recent_days < min_recent_days or c.baseline_days < min_baseline_days:
            unmeasured += 1
            continue
        if c.recent < min_recent:
            continue
        ranked.append(surge(c))
    ranked.sort(key=lambda s: (-s.score, -s.recent, s.instrument_id))
    return ranked[:top], unmeasured
