"""Whether searches for a company's stock have surged, from one fetch of its trend.

Pure. Takes a fetch's series (day to ratio, the provider's own scale) and the
trading sessions it may be read over — sessions that ended before the moment
asked about — and compares the last few sessions with the ones before them.

**Sessions, not days.** Searches for a stock collapse at the weekend: Samsung
Electronics ran at about a tenth of its weekday level on a Saturday. A window
of calendar days would call every Monday a surge.

**A missing day is zero.** The provider leaves out days with too few
searches to measure. Within a fetch that has any points, those days were
quiet, not unknown.

**The ratio of means is what survives the scaling.** Every fetch is scaled so
its busiest day is 100, so levels mean nothing across fetches; the recent
mean over the baseline mean is the same whatever the scale. One point on that
scale is added to both, so that a company going from almost nothing to
slightly more is not a thousandfold surge.

The window lengths and the smoothing are priors, versioned with every row.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date


class Status:
    MEASURED = "MEASURED"
    UNMEASURED = "UNMEASURED"
    # The newest fetch stops before the last session: the morning fetch failed.
    STALE = "STALE"
    NO_FETCH = "NO_FETCH"


@dataclass(frozen=True, slots=True)
class AttentionParams:
    version: int = 1
    recent_sessions: int = 3
    baseline_sessions: int = 20
    smoothing: float = 1.0


DEFAULT = AttentionParams()


@dataclass(frozen=True, slots=True)
class Attention:
    status: str
    surge: float | None = None
    recent: float | None = None
    baseline: float | None = None


def surge(
    series: Mapping[str, float],
    sessions: Sequence[date],
    params: AttentionParams = DEFAULT,
) -> Attention:
    """Recent searches over earlier ones, across the last sessions of `sessions` (oldest first).

    UNMEASURED when the fetch has no points at all, or does not reach back
    over enough sessions to have a baseline.
    """
    needed = params.recent_sessions + params.baseline_sessions
    if not series or len(sessions) < needed:
        return Attention(Status.UNMEASURED)
    window = sessions[-needed:]
    values = [float(series.get(day.isoformat(), 0.0)) for day in window]
    baseline = statistics.fmean(values[: params.baseline_sessions])
    recent = statistics.fmean(values[params.baseline_sessions :])
    ratio = (recent + params.smoothing) / (baseline + params.smoothing)
    return Attention(Status.MEASURED, ratio, recent, baseline)
