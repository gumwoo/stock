"""트럼프 관세 글 → 다음 한국 거래일의 갭과 첫 1시간. 순수. 한국 가격과 대조하기 전에 커밋한다.

**질문(계획 v2, 검증 차단 0건).** 트럼프가 한국 장이 닫힌 사이 트루스소셜에 관세 글을 올린 다음 한국 거래일은, 같은
달의 비슷한 날(요일·창 길이를 맞춘)과 비교해 (1) 시장 전체 갭이 다른가, (2) 9시에 사서 10시 전에 팔면(소유자 매매,
매수만) 더 나은가.

**이미 본 것.** 아카이브의 글 수·시각·본문 형식만 봤다. 널리 보도된 날은 이미 안다: 2025-04-07·04-10(관세 발표·유예
뒤 아시아 급락·급등), 2026-01-27(한국 25% 위협 뒤 코스피가 낮게 열고 사상 최고로 마감). 동기 사례로 적고 이 날들을
뺀 값을 탐색으로 함께 본다.

**표본.**
- 창: 전 거래일 장 마감(보통 15:30 KST) 이상, D 09:00 KST 미만(`window`).
- 관세 글: URL을 지운 본문에 `tariff`(대소문자 무시). 빈 본문 제외(`is_tariff`). 신호일 = 창에 관세 글 1개 이상.
- 종목일: g = 09:00 시가/전날 종가 - 1, f = 09:00 봉 종가/09:00 시가 - 1. 전날 종가·09:00 봉이 없으면 뺀다. |g| > 30.1%,
  g ≥ 29.5% 제외. 하루 값 = 그날 쓸 수 있는 종목 평균. **1,000종목 미만인 날은 관측하지 않는다**(`day_value`).
- 창 길이 18시간 초과는 "긴 창"(주말·연휴).
- 평가 2024-09-26~2026-09-23, 마지막 60거래일 홀드아웃.

**판정(`judge`).** 하루 하나를 관측으로 y = a + b·신호 + 연-월 고정효과 + 긴 창 더미 + 월요일 더미, ISO 주 군집(CR1)
표준오차. 관측이 없거나 다른 열과 같아진 더미·고정효과 열은 빼고 적합한다(`ols_cluster`).
- T1 y = 하루 g, |t| ≥ 2(방향은 보고만. 위협·완화 글이 섞인다).
- T2 신호일 하루 f - 0.30%의 평균 > 0, 주 군집 t ≥ 2(상수항만의 회귀). 유니버스 등가중은 살 대상이 아니라 "그날 분위기".
- T3 y = 하루 f, b > 0, t ≥ 2.
성립: 연구 구간 신호일 ≥ 20·비교일 ≥ 20, t 조건, 연구 구간 신호일을 날짜순으로 세운 len//2번째(0부터) 날짜 미만이 앞
절반·그 날부터가 뒤 절반이고 두 절반의 b(T2는 평균)가 연구 구간 값과 같은 방향, 홀드아웃에서도 같은 방향(t 조건
없음). **T2와 T3가 모두 성립할 때만 포워드 기록 후보.** 이번 결과로 규칙을 만들지 않는다.

**읽는 법.** 관세 글에는 위협·완화·관세 수입 자랑·기사 링크가 섞인다. 결과가 0이어도 "관세 위협이 영향이 없다"로
읽지 않는다. "관세 글이 있던 밤"이라는 조건의 질문이다.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

STUDY_VERSION = 1
SEOUL = ZoneInfo("Asia/Seoul")
EVAL_FIRST = date(2024, 9, 26)
EVAL_LAST = date(2026, 9, 23)
HOLDOUT_SESSIONS = 60
OPEN_CUTOFF = time(9, 0)
LONG_WINDOW = timedelta(hours=18)
MIN_NAMES = 1_000
MIN_DAYS = 20
MIN_T = 2.0
COST = 0.003
LIMIT_UP = 0.295
PRICE_LIMIT = 0.30
LIMIT_SLACK = 1e-3
MOTIVATION_DAYS = (date(2025, 4, 7), date(2025, 4, 10), date(2026, 1, 27))
IMAGE_LETTER_DAY = date(2025, 7, 8)

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_TARIFF = re.compile(r"tariff", re.IGNORECASE)


def is_tariff(content: str | None) -> bool:
    """URL을 지운 본문에 tariff가 있는가. 기사 링크 주소에만 있는 단어는 세지 않는다."""
    if not content or not content.strip():
        return False
    return bool(_TARIFF.search(_URL.sub(" ", content)))


def window(prev_close: datetime, day: date) -> tuple[datetime, datetime]:
    """[전 거래일 장 마감, D 09:00 KST). 둘 다 aware."""
    return prev_close, datetime.combine(day, OPEN_CUTOFF, tzinfo=SEOUL)


def count_in_windows(
    posts: Sequence[datetime], windows: Mapping[date, tuple[datetime, datetime]]
) -> dict[date, int]:
    """창마다 글 수. `posts`는 aware 시각."""
    ordered = sorted(posts)
    out: dict[date, int] = {}
    for d, (start, end) in windows.items():
        lo = _bisect(ordered, start)
        hi = _bisect(ordered, end)
        out[d] = hi - lo
    return out


def _bisect(xs: Sequence[datetime], x: datetime) -> int:
    lo, hi = 0, len(xs)
    while lo < hi:
        mid = (lo + hi) // 2
        if xs[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


@dataclass(frozen=True, slots=True)
class Day:
    day: date
    signal: bool
    posts: int
    long: bool
    monday: bool
    g: float
    f: float
    names: int


def day_value(
    names: Sequence[tuple[float, float, float]], *, min_names: int = MIN_NAMES
) -> tuple[float, float, int] | None:
    """(전날 종가, 09:00 시가, 09:00 봉 종가) 목록 → (평균 g, 평균 f, 쓴 종목 수). 모자라면 None."""
    gs: list[float] = []
    fs: list[float] = []
    for prev, open9, close10 in names:
        # yfinance 파일에는 NaN 가격이 섞여 있다. 없는 값으로 보고 뺀다(비교 연산만으로는 NaN이 걸러지지 않는다).
        if (
            not all(math.isfinite(v) for v in (prev, open9, close10))
            or min(prev, open9, close10) <= 0
        ):
            continue
        g = open9 / prev - 1
        if abs(g) > PRICE_LIMIT + LIMIT_SLACK or g >= LIMIT_UP:
            continue
        gs.append(g)
        fs.append(close10 / open9 - 1)
    if len(gs) < min_names:
        return None
    return statistics.fmean(gs), statistics.fmean(fs), len(gs)


def holdout_days(days: Sequence[date]) -> set[date]:
    return set(sorted(days)[-HOLDOUT_SESSIONS:])


def split_date(signal_days: Sequence[date]) -> date:
    """연구 구간 신호일을 날짜순으로 세운 len//2번째(0부터). 이 날짜부터가 뒤 절반."""
    ordered = sorted(signal_days)
    return ordered[len(ordered) // 2]


# --- 회귀 ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fit:
    b: float | None
    t: float | None
    n: int
    clusters: int
    dropped: tuple[str, ...]


def ols_cluster(
    y: Sequence[float],
    columns: Mapping[str, Sequence[float]],
    clusters: Sequence[object],
    *,
    target: str,
) -> Fit:
    """상수항 + `columns`로 OLS, 군집(CR1) 표준오차. `target` 열의 계수와 t.

    관측이 없는(값이 모두 같은) 열과 앞 열들로 설명되는 열은 빼고 적합한다. 탐색 하위 표본에서 월요일이 아닌 긴
    창이 0일이면 두 더미가 같아지는 경우처럼, 특이 행렬로 멈추지 않게 하기 위해서다. `target`이 빠지면 b는 None.
    """
    n = len(y)
    if n == 0:
        return Fit(None, None, 0, 0, ())
    names = ["const"]
    cols = [np.ones(n)]
    dropped: list[str] = []
    for name, values in columns.items():
        v = np.asarray(values, dtype=float)
        if np.ptp(v) == 0:
            dropped.append(name)
            continue
        trial = np.column_stack([*cols, v])
        if np.linalg.matrix_rank(trial) < trial.shape[1]:
            dropped.append(name)
            continue
        names.append(name)
        cols.append(v)
    if target not in names:
        return Fit(None, None, n, len(set(clusters)), tuple(dropped))
    x = np.column_stack(cols)
    yv = np.asarray(y, dtype=float)
    xtx_inv = np.linalg.inv(x.T @ x)
    beta = xtx_inv @ x.T @ yv
    resid = yv - x @ beta
    groups: dict[object, list[int]] = {}
    for i, c in enumerate(clusters):
        groups.setdefault(c, []).append(i)
    k = x.shape[1]
    g = len(groups)
    meat = np.zeros((k, k))
    for idx in groups.values():
        s = x[idx].T @ resid[idx]
        meat += np.outer(s, s)
    if g < 2 or n <= k:
        return Fit(float(beta[names.index(target)]), None, n, g, tuple(dropped))
    scale = g / (g - 1) * (n - 1) / (n - k)
    cov = scale * xtx_inv @ meat @ xtx_inv
    j = names.index(target)
    se = math.sqrt(cov[j, j]) if cov[j, j] > 0 else 0.0
    b = float(beta[j])
    return Fit(b, b / se if se else None, n, g, tuple(dropped))


def _week(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return iso.year, iso.week


def fit_difference(
    days: Sequence[Day], field: str, *, extra: Mapping[str, Sequence[float]] | None = None
) -> Fit:
    """y = a + b·신호 + 연-월 FE + 긴 창 + 월요일 (+ `extra`, 탐색용 통제), ISO 주 군집."""
    columns: dict[str, Sequence[float]] = {"signal": [float(d.signal) for d in days]}
    months = sorted({(d.day.year, d.day.month) for d in days})
    for ym in months[1:]:
        columns[f"m{ym[0]}-{ym[1]:02d}"] = [float((d.day.year, d.day.month) == ym) for d in days]
    columns["long"] = [float(d.long) for d in days]
    columns["monday"] = [float(d.monday) for d in days]
    columns.update(extra or {})
    y = [getattr(d, field) for d in days]
    return ols_cluster(y, columns, [_week(d.day) for d in days], target="signal")


def fit_mean(days: Sequence[Day], *, cost: float = COST) -> Fit:
    """신호일 f - 비용의 평균과 주 군집 t(상수항만)."""
    sig = [d for d in days if d.signal]
    y = [d.f - cost for d in sig]
    return ols_cluster(y, {}, [_week(d.day) for d in sig], target="const")


# --- 판정 ---------------------------------------------------------------------------------

QUESTIONS = (
    ("T1", "관세 글이 있던 밤 다음 날 유니버스 갭이 비교일과 다르다(같은 달·요일·창 조건)"),
    (
        "T2",
        "관세 글이 있던 밤 다음 날 9시에 사서 10시 직전에 팔면 비용 0.30% 뒤에 남는다(유니버스 등가중)",
    ),
    ("T3", "관세 글이 있던 밤 다음 날 첫 1시간이 비교일보다 낫다(같은 달·요일·창 조건)"),
)


@dataclass(frozen=True, slots=True)
class Verdict:
    key: str
    text: str
    signal_days: int
    comparison_days: int
    study: Fit
    halves: tuple[Fit, Fit]
    holdout: Fit
    state: str


def _fit(key: str, days: Sequence[Day], cost: float) -> Fit:
    if key == "T1":
        return fit_difference(days, "g")
    if key == "T2":
        return fit_mean(days, cost=cost)
    return fit_difference(days, "f")


def judge(days: Sequence[Day], *, cost: float = COST) -> list[Verdict]:
    held = holdout_days([d.day for d in days])
    study = [d for d in days if d.day not in held]
    hold = [d for d in days if d.day in held]
    sig_days = [d.day for d in study if d.signal]
    n_sig, n_cmp = len(sig_days), sum(1 for d in study if not d.signal)
    cut = split_date(sig_days) if sig_days else None
    front = [d for d in study if cut is not None and d.day < cut]
    back = [d for d in study if cut is not None and d.day >= cut]
    out = []
    for key, text in QUESTIONS:
        s = _fit(key, study, cost)
        halves = (_fit(key, front, cost), _fit(key, back, cost))
        h = _fit(key, hold, cost)
        state = "not established"
        if n_sig < MIN_DAYS or n_cmp < MIN_DAYS or s.b is None or s.t is None:
            state = "not enough days"
        else:
            sign = 1.0 if s.b > 0 else -1.0
            strong = abs(s.t) >= MIN_T if key == "T1" else (s.b > 0 and s.t >= MIN_T)
            same = all(f.b is not None and f.b * sign > 0 for f in (*halves, h))
            if strong and same:
                state = "established"
        out.append(Verdict(key, text, n_sig, n_cmp, s, halves, h, state))
    return out


def candidate(verdicts: Sequence[Verdict]) -> bool:
    ok = {v.key for v in verdicts if v.state == "established"}
    return {"T2", "T3"} <= ok
