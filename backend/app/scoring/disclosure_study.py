"""공시 이벤트 분석: 사건 공시가 난 종목의 다음 거래일을, 데이터를 보기 전에 정한 질문으로 잰다. 순수.

**무엇을 재나.** D일에 접수된 사건 공시는 D 다음 첫 거래일(진입일) 아침에 본다. 장전 관찰 목록
(`PREOPEN_V2`)이 공시를 읽는 규칙과 같다. 접수 시각은 모르므로 D일 장중에 이미 반영된 반응은
잡히지 않는다. 운영과 같은 조건으로 잰다는 뜻이다.

**질문은 이 모듈이 데이터보다 먼저 커밋됐다.** 결과를 본 뒤 질문이나 기준을 고치면 그건 새 버전이다.

- D1 사건 공시 종목은 진입일 시가→종가에서 자기 지수를 이긴다
- D2 방향이 정해진 공시는 진입일 시가→종가에서 그 방향으로 움직인다(지수 대비)
- D3 방향이 정해진 공시는 진입일 갭(전날 종가→시가)이 그 방향이다(지수 대비)
- D4 강도가 높은 공시(0.5 이상)는 낮은 공시보다 진입일에 크게 움직인다(지수 대비 절댓값)

**날짜별로 짝짓는다.** 진입일마다 그날 종목들의 평균(D4는 두 그룹 평균의 차)이 관측 하나다.
한 날에 여러 종목이 몰려도 그날은 하나로 센다.

**홀드아웃.** 진입일 중 마지막 `HOLDOUT_SESSIONS`개는 떼어 둔다. 판정은 앞부분(연구 구간)에서
평균이 양수이고 t ≥ 2이고 앞·뒤 절반이 모두 양수이며, **홀드아웃 평균도 양수**일 때만 "성립"이다.
홀드아웃은 한 번만 읽는다. 공시 종류별 표는 탐색용이고 판정하지 않는다.

**알고 두는 한계.** 표본은 지금 상장된 KOSPI·KOSDAQ 종목뿐이다. 그사이 상장폐지된 회사가 빠지므로
나쁜 공시의 효과는 실제보다 덜 나쁘게 보일 수 있다. 3개월은 한 가지 시장 분위기다.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

STUDY_VERSION = 1
HOLDOUT_SESSIONS = 15
MIN_T = 2.0
HIGH_INTENSITY = 0.5


@dataclass(frozen=True, slots=True)
class EventDay:
    """사건 공시가 난 종목의 진입일 하나. 같은 날 여러 공시면 가장 강한 것 하나로 줄인다."""

    day: date
    instrument_id: int
    event_type: str
    sentiment: float | None
    """공시 분류 규칙의 방향 사전값. 제목으로 방향을 알 수 없으면 None."""
    intensity: float
    gap: float
    """전날 종가 → 진입일 시가, 비율."""
    open_close: float
    """진입일 시가 → 종가, 비율."""
    index_gap: float
    index_open_close: float

    @property
    def excess(self) -> float:
        return self.open_close - self.index_open_close

    @property
    def excess_gap(self) -> float:
        return self.gap - self.index_gap


@dataclass(frozen=True, slots=True)
class Question:
    key: str
    text: str
    daily: Callable[[Sequence[EventDay]], float | None]
    """그날의 종목들로 관측 하나를 만든다. 만들 수 없으면 None."""


def _sign(x: float | None) -> int:
    return 0 if not x else (1 if x > 0 else -1)


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _d1(events: Sequence[EventDay]) -> float | None:
    return _mean([e.excess for e in events])


def _d2(events: Sequence[EventDay]) -> float | None:
    return _mean([_sign(e.sentiment) * e.excess for e in events if _sign(e.sentiment)])


def _d3(events: Sequence[EventDay]) -> float | None:
    return _mean([_sign(e.sentiment) * e.excess_gap for e in events if _sign(e.sentiment)])


def _d4(events: Sequence[EventDay]) -> float | None:
    high = _mean([abs(e.excess) for e in events if e.intensity >= HIGH_INTENSITY])
    low = _mean([abs(e.excess) for e in events if e.intensity < HIGH_INTENSITY])
    return None if high is None or low is None else high - low


QUESTIONS: tuple[Question, ...] = (
    Question("D1", "사건 공시 종목이 진입일 시가→종가에서 지수를 이긴다", _d1),
    Question("D2", "방향이 정해진 공시가 진입일에 그 방향으로 움직인다(지수 대비)", _d2),
    Question("D3", "방향이 정해진 공시의 진입일 갭이 그 방향이다(지수 대비)", _d3),
    Question("D4", "강도 높은 공시가 낮은 공시보다 크게 움직인다(지수 대비 절댓값)", _d4),
)


@dataclass(frozen=True, slots=True)
class Answer:
    key: str
    text: str
    days: int
    mean: float | None
    t: float | None
    halves: tuple[float | None, float | None]
    holdout_days: int
    holdout_mean: float | None
    state: str
    """established / not established / not enough days"""


def mean_t(values: Sequence[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, None
    se = statistics.stdev(values) / math.sqrt(len(values))
    return mean, (mean / se if se else None)


def split(days: Sequence[date]) -> tuple[list[date], list[date]]:
    """진입일을 연구 구간과 홀드아웃(마지막 `HOLDOUT_SESSIONS`개)으로 나눈다."""
    ordered = sorted(set(days))
    if len(ordered) <= HOLDOUT_SESSIONS:
        return ordered, []
    return ordered[:-HOLDOUT_SESSIONS], ordered[-HOLDOUT_SESSIONS:]


def _by_day(events: Sequence[EventDay]) -> dict[date, list[EventDay]]:
    out: dict[date, list[EventDay]] = defaultdict(list)
    for e in events:
        out[e.day].append(e)
    return out


def evaluate(events: Sequence[EventDay], *, min_days: int = 20) -> list[Answer]:
    by_day = _by_day(events)
    study, holdout = split(list(by_day))
    answers = []
    for q in QUESTIONS:
        obs = [v for d in study if (v := q.daily(by_day[d])) is not None]
        held = [v for d in holdout if (v := q.daily(by_day[d])) is not None]
        mean, t = mean_t(obs)
        half = len(obs) // 2
        halves = (mean_t(obs[:half])[0], mean_t(obs[half:])[0])
        held_mean = mean_t(held)[0]
        if len(obs) < min_days or not held:
            state = "not enough days"
        elif (
            mean is not None
            and t is not None
            and mean > 0
            and t >= MIN_T
            and all(h is not None and h > 0 for h in halves)
            and held_mean is not None
            and held_mean > 0
        ):
            state = "established"
        else:
            state = "not established"
        answers.append(
            Answer(q.key, q.text, len(obs), mean, t, halves, len(held), held_mean, state)
        )
    return answers


@dataclass(frozen=True, slots=True)
class TypeRow:
    """공시 종류별 탐색 표의 한 줄. 판정하지 않는다."""

    event_type: str
    events: int
    days: int
    mean_excess: float | None
    t: float | None
    up_share: float | None
    """지수를 이긴 종목일의 비율."""


def by_type(events: Sequence[EventDay]) -> list[TypeRow]:
    groups: dict[str, list[EventDay]] = defaultdict(list)
    for e in events:
        groups[e.event_type].append(e)
    rows = []
    for kind, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        daily = [statistics.fmean(e.excess for e in day) for day in _by_day(members).values()]
        mean, t = mean_t(daily)
        rows.append(
            TypeRow(
                kind,
                len(members),
                len(daily),
                mean,
                t,
                sum(1 for e in members if e.excess > 0) / len(members),
            )
        )
    return rows


def strongest(candidates: Sequence[EventDay]) -> EventDay:
    """같은 종목·같은 진입일의 공시 여럿 중 하나: 강도가 크고, 방향이 있는 것을 먼저."""
    return max(candidates, key=lambda e: (e.intensity, abs(e.sentiment or 0.0), e.event_type))
