"""What the morning lists are to be judged on, fixed before any list has an outcome.

Pure. Five questions, each a difference between two groups of the morning's
names on the same day, measured open to close (or over the first hour) from
the minute bars:

- H1 names in for good news beat the same day's list average
- H2 names in for bad news fall behind it
- H3 names in for a search surge do better in the first hour than the rest
- H4 ranks 1-10 beat ranks 11-40
- H5 the list as a whole beats its market, name by name against its own index

H6·H7은 2026-09-26, 첫 목록(9/28 08:50)이 나오기 전에 더했다. 소유자의 단타는 9시 시가에 사서
늦어도 10시 전에 판다. 그 시간대로 묻는다(첫 1시간 수익률, 비용 전).
- H6 공시 이유로 들어간 종목을 9시 시가에 사서 10시 전에 팔면 평균이 0보다 크다
- H7 목록 전체를 같은 방식으로 사고팔면 평균이 0보다 크다
과거 3개월로 비슷한 질문을 잰 공시 이벤트 분석 v2(`disclosure_first_hour`)와 함께 읽는다. 다만
여기 첫 1시간은 그날 첫 봉의 시가에서 재므로(첫 봉이 09:02인 날도 들어간다) v2와 모집단이 다르다.

**Paired by day.** On each day with both groups present, the difference of the
two groups' means is one observation; the statistic is over those days. A day
where one group is absent says nothing about the difference and is left out.

**The gates are the forward record's**: 20 days to read, 60 to decide — on
the first sixty days, a fixed sample, once — and a question is established
only if the mean daily difference is positive with a t of at least 2 and
positive in both halves of those days. Established is not
acted on: it is what a later strategy version may be proposed from.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

READ_DAYS = 20
DECIDE_DAYS = 60
MIN_T = 2.0


@dataclass(frozen=True, slots=True)
class MemberDay:
    """One morning-list name on its day, and what the day did."""

    day: date
    rank: int
    reasons: tuple[str, ...]
    regime: str | None
    return_pct: float
    first_hour_pct: float | None
    market_return_pct: float | None


Select = Callable[[MemberDay], bool]
Measure = Callable[[MemberDay], float | None]


@dataclass(frozen=True, slots=True)
class Hypothesis:
    key: str
    text: str
    group: Select
    against: Select | None
    """None: against the whole list that day."""
    measure: Measure
    vs_zero: bool = False
    """그룹 평균 자체를 0과 비교한다. 측정값이 이미 차이(H5)이거나 수익 그 자체(H6·H7)일 때."""


def _open_close(m: MemberDay) -> float | None:
    return m.return_pct


def _first_hour(m: MemberDay) -> float | None:
    return m.first_hour_pct


def _vs_market(m: MemberDay) -> float | None:
    return None if m.market_return_pct is None else m.return_pct - m.market_return_pct


HYPOTHESES: tuple[Hypothesis, ...] = (
    Hypothesis(
        "H1",
        "good news beats the day's list",
        lambda m: "POSITIVE_NEWS_OVERLAY" in m.reasons,
        None,
        _open_close,
    ),
    Hypothesis(
        "H2",
        "the day's list beats bad news",
        lambda m: True,
        lambda m: "NEGATIVE_NEWS_OVERLAY" in m.reasons,
        _open_close,
    ),
    Hypothesis(
        "H3",
        "search surge does better in the first hour",
        lambda m: "SEARCH_SURGE" in m.reasons,
        lambda m: "SEARCH_SURGE" not in m.reasons,
        _first_hour,
    ),
    Hypothesis(
        "H4",
        "ranks 1-10 beat ranks 11-40",
        lambda m: m.rank <= 10,
        lambda m: m.rank > 10,
        _open_close,
    ),
    Hypothesis(
        "H5",
        "the list beats its market",
        lambda m: True,
        None,
        _vs_market,
        vs_zero=True,
    ),
    Hypothesis(
        "H6",
        "공시 이유 종목, 9시 시가에 사서 10시 전에 팔면 평균이 0보다 크다",
        lambda m: "DISCLOSURE_EVENT" in m.reasons,
        None,
        _first_hour,
        vs_zero=True,
    ),
    Hypothesis(
        "H7",
        "목록 전체, 9시 시가에 사서 10시 전에 팔면 평균이 0보다 크다",
        lambda m: True,
        None,
        _first_hour,
        vs_zero=True,
    ),
)


@dataclass(frozen=True, slots=True)
class Result:
    key: str
    text: str
    days: int
    mean: float | None
    t: float | None
    halves: tuple[float | None, float | None]
    state: str
    """not enough days / reading / established / not established"""


def _daily(members: Sequence[MemberDay], hyp: Hypothesis) -> dict[date, float]:
    by_day: dict[date, list[MemberDay]] = defaultdict(list)
    for m in members:
        by_day[m.day].append(m)
    out: dict[date, float] = {}
    for day, names in by_day.items():
        mine = [v for m in names if hyp.group(m) and (v := hyp.measure(m)) is not None]
        if hyp.vs_zero:
            # Against zero: the measure is already an excess (H5) or the return itself (H6, H7).
            if mine:
                out[day] = statistics.fmean(mine)
            continue
        other_pool = names if hyp.against is None else [m for m in names if hyp.against(m)]
        others = [v for m in other_pool if (v := hyp.measure(m)) is not None]
        if mine and others:
            out[day] = statistics.fmean(mine) - statistics.fmean(others)
    return out


def _mean_t(values: Sequence[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, None
    se = statistics.stdev(values) / math.sqrt(len(values))
    return mean, (mean / se if se else None)


def evaluate(members: Sequence[MemberDay]) -> list[Result]:
    results = []
    for hyp in HYPOTHESES:
        daily = _daily(members, hyp)
        ordered = [daily[d] for d in sorted(daily)]
        # Decided once, on the first sixty days: asking again every day after
        # would be asking until chance says yes.
        ordered = ordered[:DECIDE_DAYS]
        mean, t = _mean_t(ordered)
        half = len(ordered) // 2
        halves = (_mean_t(ordered[:half])[0], _mean_t(ordered[half:])[0])
        if len(ordered) < READ_DAYS:
            state = "not enough days"
        elif len(ordered) < DECIDE_DAYS:
            state = "reading"
        elif (
            mean is not None
            and t is not None
            and mean > 0
            and t >= MIN_T
            and all(h is not None and h > 0 for h in halves)
        ):
            state = "established"
        else:
            state = "not established"
        results.append(Result(hyp.key, hyp.text, len(ordered), mean, t, halves, state))
    return results
