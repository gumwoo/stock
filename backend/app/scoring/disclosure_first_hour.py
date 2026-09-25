"""공시 이벤트 분석 v2: 9시 시가에 사서 10시 전에 파는 시간대로 잰다. 순수.

v1(`disclosure_study`)은 진입일 시가→종가를 쟀다. 소유자의 단타는 9시에 사서 9시 1분부터 팔기
시작해 늦어도 10시 전에 끝나므로, 그 시간대의 질문을 따로 둔다. v1과 같은 표본(사건 공시 종목의
진입일)과 같은 홀드아웃 규칙을 쓰고, **1분봉을 받기 전에 이 모듈을 커밋했다.** v1의 일봉 결과는 이미
봤으므로 이 결과는 과거 3개월만으로 결론을 내지 않고, 앞으로의 목록 기록(H6·H7)과 함께 읽는다.

**값의 정의.**
- 진입가: 09:00 봉의 시가(= 장 시작 단일가). 첫 1시간 수익률은 장중 분석(`intraday.first_hour_pct`)과
  같게 진입가 → 10시 전 마지막 봉의 종가다.
- 1시간 안의 최대 상승·하락(MFE·MAE): **09:01~09:59 봉**의 고가·저가로 잰다. 09:00 봉의 고가·저가에는
  시가 단일가가 섞여 있어 9시 1분부터 파는 사람이 받을 수 있는 값이 아니다. 둘 다 사후 값이다.
- 거래 비용(수수료, 세금)은 빼지 않은 값이다. 판정 기준 0은 비용 전이다.

**질문.** 지수의 과거 분봉은 받을 수 없어 지수 대비로 재지 않는다(F1·F4는 절대값, F2·F3은 같은 날
종목끼리 비교).
- F1 공시 종목을 9시 시가에 사서 10시 전 종가에 팔면 평균이 0보다 크다
- F2 좋은 공시(방향 +)가 나쁜 공시(방향 -)보다 첫 1시간에 낫다
- F3 강한 공시(0.5 이상)가 약한 공시보다 첫 1시간에 크게 움직인다(MFE - MAE, 폭)
- F4 첫 1시간 안의 최대 상승폭이 최대 하락폭보다 크다(MFE + MAE > 0)

판정은 v1과 같다: 날짜별로 짝짓고, 마지막 15거래일은 홀드아웃, 연구 구간 t ≥ 2와 앞·뒤 절반 양수,
홀드아웃 양수.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from app.scoring.disclosure_study import HIGH_INTENSITY, MIN_T, Answer, mean_t, split

STUDY_VERSION = 2
OPEN_LABEL = "0900"
FIRST_SELLABLE = "0901"
HOUR_END = "1000"


@dataclass(frozen=True, slots=True)
class MinuteBar:
    label: str
    """HHMM, 그 분의 시작."""
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class FirstHour:
    day: date
    instrument_id: int
    event_type: str
    sentiment: float | None
    intensity: float
    ret: float
    """진입가 → 10시 전 마지막 종가, 비율."""
    mfe: float
    mae: float


def measure(bars: Sequence[MinuteBar]) -> tuple[float, float, float] | None:
    """(첫 1시간 수익률, MFE, MAE). 09:00 봉이나 팔 수 있는 봉이 없으면 None."""
    hour = sorted((b for b in bars if OPEN_LABEL <= b.label < HOUR_END), key=lambda b: b.label)
    if not hour or hour[0].label != OPEN_LABEL or hour[0].open <= 0:
        return None
    sellable = [b for b in hour if b.label >= FIRST_SELLABLE]
    if not sellable:
        return None
    entry = hour[0].open
    return (
        hour[-1].close / entry - 1,
        max(b.high for b in sellable) / entry - 1,
        min(b.low for b in sellable) / entry - 1,
    )


def _sign(x: float | None) -> int:
    return 0 if not x else (1 if x > 0 else -1)


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _f1(day: Sequence[FirstHour]) -> float | None:
    return _mean([e.ret for e in day])


def _f2(day: Sequence[FirstHour]) -> float | None:
    good = _mean([e.ret for e in day if _sign(e.sentiment) > 0])
    bad = _mean([e.ret for e in day if _sign(e.sentiment) < 0])
    return None if good is None or bad is None else good - bad


def _f3(day: Sequence[FirstHour]) -> float | None:
    high = _mean([e.mfe - e.mae for e in day if e.intensity >= HIGH_INTENSITY])
    low = _mean([e.mfe - e.mae for e in day if e.intensity < HIGH_INTENSITY])
    return None if high is None or low is None else high - low


def _f4(day: Sequence[FirstHour]) -> float | None:
    return _mean([e.mfe + e.mae for e in day])


@dataclass(frozen=True, slots=True)
class Question:
    key: str
    text: str
    daily: Callable[[Sequence[FirstHour]], float | None]


QUESTIONS: tuple[Question, ...] = (
    Question("F1", "공시 종목을 9시 시가에 사서 10시 전에 팔면 평균이 0보다 크다(비용 전)", _f1),
    Question("F2", "좋은 공시가 나쁜 공시보다 첫 1시간에 낫다", _f2),
    Question("F3", "강한 공시가 약한 공시보다 첫 1시간에 크게 움직인다(MFE - MAE)", _f3),
    Question("F4", "첫 1시간 안의 최대 상승폭이 최대 하락폭보다 크다", _f4),
)


def evaluate(events: Sequence[FirstHour], *, min_days: int = 20) -> list[Answer]:
    by_day: dict[date, list[FirstHour]] = defaultdict(list)
    for e in events:
        by_day[e.day].append(e)
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
