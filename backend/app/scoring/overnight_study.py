"""밤사이 미국 업종 지표 → 다음 날 한국 "지표 연동 종목"의 첫 1시간. 순수. 데이터(한국)보다 먼저 커밋했다.

**질문.** 미국 업종 지표가 밤사이 크게 오른 다음 날, 그 지표와 과거에 같이 움직인 한국 종목을 9시 첫 체결가에 사서
10시 전에 팔면(소유자의 매매, 매수만) 비용을 빼고도 남는가, 그리고 그 종목의 평소보다 나은가.

**설계(계획 v4, 검증 에이전트 차단 0건).**
1. 미국 수익률 정렬: 한국 거래일 D의 r = (D 09:00 KST 전 마지막 미국 종가) / (D-1 한국 장 마감 전 마지막 미국 종가)
   - 1. 그 사이 새 미국 세션이 없으면 신호 없음, 여럿이면 누적. 학습·평가에 같은 함수(`align`).
2. 잔차: e = r_지표 - beta·r_QQQ, beta는 학습 구간 회귀로 한 번(`fit_beta`).
3. 큰 밤: e_D ≥ 2·sigma_D. sigma_D는 D 이전에 e가 있는 최근 60개 한국 거래일 e의 표준편차(과거 값만, `trailing_sigma`).
4. 지표 연동 종목(학습 구간만): 종목의 "갭 - 유니버스 등가중 갭"과 e의 상관이 t ≥ 3인 종목 중 상위 15(`select`).
   품질 검사: 학습 앞 절반으로 고른 목록이 5종목 이상이고, 그 등가중 묶음이 뒤 절반에서 e와 t ≥ 2로 상관해야
   지표를 남긴다(`quality`). 최종 목록은 학습 구간 전체로 고른다.
5. 기준은 지수가 아니라 유니버스 등가중. 갭이 전날 종가 대비 ±30%(여유 0.1%p)를 넘으면 데이터 흔적으로 뺀다.
   평가일 진입가는 60분봉 09:00 봉 시가(시가 변동성 완화장치로 첫 체결이 늦은 날도 그 첫 체결가), 갭 ≥ +29.5%는
   상한가라 살 수 없어 뺀다. 09:00 봉이 없는 날(10시 개장)은 그날을 뺀다.

**질문(매수 쪽, 오른 큰 밤 다음 날).** 관측은 신호 있는 평가일 하나. 그날 큰 밤인 지표마다 목록 종목 평균을 구하고,
지표별 평균을 등가중으로 평균한다.
- O1 신호 종목의 갭 - 등가중 갭 > 0 (신호가 실재하는가)
- O2 신호 종목의 첫 1시간 - 등가중 첫 1시간 > 0 (시장보다 나은가)
- O3 신호 종목의 첫 1시간 - 0.30% > 0 (비용 뒤. 0.30%는 매도 세금 0.20% + 수수료·슬리피지 가정)
- O4 신호 종목의 첫 1시간 - 그 종목의 평소 > 0. 평소 = D 이전 60개 한국 거래일 중 보통 날(미국 새 세션이 있었고,
  그 종목이 속한 모든 지표에서 |e_{D'}| < 2sigma_{D'})의 첫 1시간 평균. 보통 날 20일 미만이면 O4에서 뺀다.

**판정.** 평가 구간(2025-09-22~2026-09-23)의 마지막 60거래일은 홀드아웃. 연구 구간 관측 20일 이상, 평균 > 0,
t ≥ 2, 앞·뒤 절반 모두 > 0, 홀드아웃 평균 > 0. **V3는 O3와 O4가 모두 성립할 때만.**

**미리 적어 둔 것.** 예상 관측 수(미국 데이터만, 품질 검사 전 지표 7개 기준): 연구 35일, 홀드아웃 8일. 품질 검사 뒤
지표 구성으로 다시 세어 보고한다. 홀드아웃은 작아 확인용일 뿐이고, 동기 사례 9/7은 이 규칙에서 신호가 아니다
(SOX 잔차 1.72sigma). V3를 만들면 병행 기록만 하고, 운영 신호일 20일(대략 5~7개월, 예측)이 쌓이면 같은 O3·O4를
연구와 같은 방식(yfinance 60분봉 09:00 봉, 과거 60일 기준값)으로 한 번 다시 판정한다(홀드아웃 항목 없음). 그때
실패하면 V3 기록을 멈춘다.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

STUDY_VERSION = 1
INDICATORS = ("^SOX", "IGV", "XBI", "LIT", "URA", "XLE", "TAN")
MARKET = "QQQ"
TRAIN_FIRST = date(2023, 9, 21)
TRAIN_LAST = date(2025, 9, 19)
EVAL_FIRST = date(2025, 9, 22)
EVAL_LAST = date(2026, 9, 23)
HOLDOUT_SESSIONS = 60
SIGMA_WINDOW = 60
BIG = 2.0
TOP = 15
T_SELECT = 3.0
MIN_LIST = 5
T_QUALITY = 2.0
COST = 0.003
LIMIT_UP = 0.295
PRICE_LIMIT = 0.30
LIMIT_SLACK = 1e-3
NORMAL_MIN = 20
MIN_T = 2.0
MIN_DAYS = 20
MIN_TRAIN_DAYS = 200
MIN_TRADED_VALUE = 1_000_000_000  # 원, 학습 구간 평균 일 거래대금


# --- 정렬, 잔차, sigma -------------------------------------------------------------------


def align(
    kr_days: Sequence[date],
    kr_open: Mapping[date, datetime],
    kr_prev_close: Mapping[date, datetime],
    us: Sequence[tuple[datetime, float]],
) -> dict[date, float | None]:
    """한국 거래일마다 그 전날 한국 장 마감 뒤 ~ 그날 개장 전에 끝난 미국 세션들의 누적 수익률. 없으면 None."""
    ordered = sorted(us)
    out: dict[date, float | None] = {}
    for d in kr_days:
        before_open = [p for t, p in ordered if t < kr_open[d]]
        before_prev = [p for t, p in ordered if t < kr_prev_close[d]]
        if not before_open or not before_prev or len(before_open) == len(before_prev):
            out[d] = None
            continue
        out[d] = before_open[-1] / before_prev[-1] - 1
    return out


def fit_beta(
    r_ind: Mapping[date, float | None], r_mkt: Mapping[date, float | None], days: Sequence[date]
) -> float:
    pairs: list[tuple[float, float]] = []
    for d in days:
        x, y = r_mkt.get(d), r_ind.get(d)
        if x is not None and y is not None:
            pairs.append((x, y))
    mx = statistics.fmean(x for x, _ in pairs)
    my = statistics.fmean(y for _, y in pairs)
    var = sum((x - mx) ** 2 for x, _ in pairs)
    return sum((x - mx) * (y - my) for x, y in pairs) / var


def residuals(
    r_ind: Mapping[date, float | None], r_mkt: Mapping[date, float | None], beta: float
) -> dict[date, float | None]:
    out: dict[date, float | None] = {}
    for d, r in r_ind.items():
        m = r_mkt.get(d)
        out[d] = None if r is None or m is None else r - beta * m
    return out


def trailing_sigma(
    e: Mapping[date, float | None], *, window: int = SIGMA_WINDOW
) -> dict[date, float | None]:
    """D 이전에 e가 있는 최근 `window`일의 표본 표준편차. 그만큼 쌓이기 전에는 None."""
    out: dict[date, float | None] = {}
    seen: list[float] = []
    for d in sorted(e):
        out[d] = statistics.stdev(seen[-window:]) if len(seen) >= window else None
        v = e[d]
        if v is not None:
            seen.append(v)
    return out


def is_big(e: float | None, sigma: float | None) -> bool:
    return e is not None and sigma is not None and sigma > 0 and e >= BIG * sigma


def is_calm(e: float | None, sigma: float | None) -> bool:
    return e is not None and sigma is not None and sigma > 0 and abs(e) < BIG * sigma


# --- 상관, 연동 종목, 품질 검사 ---------------------------------------------------------


def corr_t(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float] | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    rho = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)
    if abs(rho) >= 1:
        return rho, math.inf
    return rho, rho * math.sqrt(n - 2) / math.sqrt(1 - rho * rho)


def select(
    e: Mapping[date, float | None],
    gap_rel: Mapping[str, Mapping[date, float]],
    days: Sequence[date],
    *,
    top: int = TOP,
    t_min: float = T_SELECT,
) -> list[tuple[str, float]]:
    """`days`에서 e와 "갭 - 등가중 갭"의 상관이 t ≥ t_min인 종목 중 상관 상위 `top`. (종목, rho)."""
    ranked = []
    for name, series in gap_rel.items():
        pairs = [(e[d], series[d]) for d in days if e.get(d) is not None and d in series]
        got = corr_t([p[0] for p in pairs], [p[1] for p in pairs])  # type: ignore[misc]
        if got is not None and got[1] >= t_min:
            ranked.append((name, got[0]))
    ranked.sort(key=lambda x: (-x[1], x[0]))
    return ranked[:top]


@dataclass(frozen=True, slots=True)
class Quality:
    indicator: str
    front_list: int
    back_rho: float | None
    back_t: float | None
    passed: bool


def quality(
    indicator: str,
    e: Mapping[date, float | None],
    gap_rel: Mapping[str, Mapping[date, float]],
    train_days: Sequence[date],
) -> Quality:
    """앞 절반 목록 ≥ 5종목이고, 그 등가중 묶음이 뒤 절반에서 e와 t ≥ 2로 상관하면 통과."""
    ordered = sorted(train_days)
    half = len(ordered) // 2
    front, back = ordered[:half], ordered[half:]
    chosen = [n for n, _ in select(e, gap_rel, front)]
    if len(chosen) < MIN_LIST:
        return Quality(indicator, len(chosen), None, None, False)
    xs, ys = [], []
    for d in back:
        if e.get(d) is None:
            continue
        vals = [gap_rel[n][d] for n in chosen if d in gap_rel[n]]
        if vals:
            xs.append(e[d])
            ys.append(statistics.fmean(vals))
    got = corr_t(xs, ys)  # type: ignore[arg-type]
    if got is None:
        return Quality(indicator, len(chosen), None, None, False)
    return Quality(indicator, len(chosen), got[0], got[1], got[1] >= T_QUALITY)


# --- 평가 표본과 판정 -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayBar:
    """평가일 종목 하나의 값: 갭(전날 종가 대비)과 첫 1시간(09:00 봉 종가/시가 - 1)."""

    gap: float
    first_hour: float


def usable(bar: DayBar) -> bool:
    return abs(bar.gap) <= PRICE_LIMIT + LIMIT_SLACK and bar.gap < LIMIT_UP


@dataclass(slots=True)
class Observation:
    day: date
    indicators: tuple[str, ...]
    values: dict[str, float | None] = field(default_factory=dict)
    """질문 키 → 지표별 평균의 등가중 평균."""
    by_indicator: dict[str, dict[str, float]] = field(default_factory=dict)
    """지표 → 질문 키 → 그 지표 목록 평균(leave-one-out 계산용)."""


def baseline(
    name: str,
    day: date,
    sessions: Sequence[date],
    bars: Mapping[str, Mapping[date, DayBar]],
    calm: Mapping[str, Mapping[date, bool]],
) -> float | None:
    """그 종목의 D 이전 60개 한국 거래일 중 보통 날의 첫 1시간 평균. 20일 미만이면 None."""
    i = sessions.index(day)
    window = sessions[max(0, i - SIGMA_WINDOW) : i]
    vals = [
        bars[name][d].first_hour
        for d in window
        if d in bars.get(name, {}) and calm.get(name, {}).get(d, False) and usable(bars[name][d])
    ]
    return statistics.fmean(vals) if len(vals) >= NORMAL_MIN else None


QUESTIONS = (
    ("O1", "신호 종목의 갭이 등가중 갭보다 크다(신호가 실재한다)"),
    ("O2", "신호 종목의 첫 1시간이 등가중 첫 1시간보다 크다"),
    ("O3", "신호 종목의 첫 1시간이 비용 0.30%를 넘는다"),
    ("O4", "신호 종목의 첫 1시간이 그 종목의 평소보다 크다"),
)


def observe(
    day: date,
    active: Sequence[str],
    lists: Mapping[str, Sequence[str]],
    bars: Mapping[str, Mapping[date, DayBar]],
    sessions: Sequence[date],
    calm: Mapping[str, Mapping[date, bool]],
) -> Observation | None:
    """그날의 관측. 유니버스 등가중은 그날 쓸 수 있는 모든 종목의 평균."""
    today = [b for series in bars.values() if (b := series.get(day)) is not None and usable(b)]
    if not today or not active:
        return None
    ew_gap = statistics.fmean(b.gap for b in today)
    ew_fh = statistics.fmean(b.first_hour for b in today)
    obs = Observation(day, tuple(active))
    for ind in active:
        members = [
            (n, bars[n][day]) for n in lists[ind] if day in bars.get(n, {}) and usable(bars[n][day])
        ]
        if not members:
            continue
        per: dict[str, float] = {
            "O1": statistics.fmean(b.gap - ew_gap for _, b in members),
            "O2": statistics.fmean(b.first_hour - ew_fh for _, b in members),
            "O3": statistics.fmean(b.first_hour - COST for _, b in members),
        }
        diffs = [
            b.first_hour - base
            for n, b in members
            if (base := baseline(n, day, sessions, bars, calm)) is not None
        ]
        if diffs:
            per["O4"] = statistics.fmean(diffs)
        obs.by_indicator[ind] = per
    if not obs.by_indicator:
        return None
    for key, _ in QUESTIONS:
        vals = [p[key] for p in obs.by_indicator.values() if key in p]
        obs.values[key] = statistics.fmean(vals) if vals else None
    return obs


@dataclass(frozen=True, slots=True)
class Verdict:
    key: str
    text: str
    days: int
    mean: float | None
    t: float | None
    halves: tuple[float | None, float | None]
    holdout_days: int
    holdout_mean: float | None
    state: str


def mean_t(values: Sequence[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    m = statistics.fmean(values)
    if len(values) < 2:
        return m, None
    se = statistics.stdev(values) / math.sqrt(len(values))
    return m, (m / se if se else None)


def holdout_days(eval_sessions: Sequence[date]) -> set[date]:
    return set(sorted(eval_sessions)[-HOLDOUT_SESSIONS:])


def judge(
    observations: Sequence[Observation], eval_sessions: Sequence[date], *, drop: str | None = None
) -> list[Verdict]:
    """O1~O4 판정. `drop`을 주면 그 지표를 뺀 값으로 다시 센다(leave-one-out)."""
    held = holdout_days(eval_sessions)
    out = []
    for key, text in QUESTIONS:
        study_vals: list[float] = []
        held_vals: list[float] = []
        for o in sorted(observations, key=lambda o: o.day):
            per = [p[key] for ind, p in o.by_indicator.items() if ind != drop and key in p]
            if not per:
                continue
            v = statistics.fmean(per)
            (held_vals if o.day in held else study_vals).append(v)
        mean, t = mean_t(study_vals)
        half = len(study_vals) // 2
        halves = (mean_t(study_vals[:half])[0], mean_t(study_vals[half:])[0])
        hm = mean_t(held_vals)[0]
        if len(study_vals) < MIN_DAYS or not held_vals:
            state = "not enough days"
        elif (
            mean is not None
            and t is not None
            and mean > 0
            and t >= MIN_T
            and all(h is not None and h > 0 for h in halves)
            and hm is not None
            and hm > 0
        ):
            state = "established"
        else:
            state = "not established"
        out.append(Verdict(key, text, len(study_vals), mean, t, halves, len(held_vals), hm, state))
    return out


def build_v3(verdicts: Sequence[Verdict]) -> bool:
    """V3를 만들지: O3와 O4가 모두 성립할 때만."""
    states = {v.key: v.state for v in verdicts}
    return states.get("O3") == "established" and states.get("O4") == "established"
