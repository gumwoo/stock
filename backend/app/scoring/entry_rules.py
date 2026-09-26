"""진입·청산 규칙: "9시 시가에 무조건 산다" 대신 규칙 세 개. 순수. 규칙 수익률을 보기 전에 커밋한다.

**질문(계획 v2, 검증 차단 0건).** 지금까지의 연구는 종목 고르기로는 9시 매수·10시 전 매도가 비용 뒤에 남는 근거를 찾지
못했다. 같은 종목에서 언제 사고 언제 파느냐를 바꾸면 남는가.

**이미 본 것.** 표본(공시 v2의 09:00~10:00 KIS 1분봉, 진입일 57일, 09:00 봉이 있는 2,545종목일)의 기준 매매 결과(F1
-0.23%, 홀드아웃 -0.68%, MFE 중앙값 +1.55%, MAE 중앙값 -2.12%, 57일 전체)를 봤다. R3의 폭(-2%/+3%)은 그 분포를 본 뒤
반올림해 골랐다(소유자 습관이 아니다). 그래서 R3의 홀드아웃은 독립적이지 않다.

**봉.** [표기, 시가, 고가, 저가, 종가], "0901" = 09:01:00~09:01:59(데이터 모양으로 본 추정). 표기 < "1000"만 쓴다. 매도가 =
10시 전 마지막 봉 종가. 진입한 봉 뒤에 봉이 없으면 진입한 봉의 종가. 빠진 봉은 체결 없음으로 보고 판단 가격은 직전
체결가를 이월한다. 호가 단위는 KRX 2023년 개편 호가표(`tick`).

**규칙.**
- R0: 09:00 봉 시가 E에 산다.
- R1: 09:04:59까지 마지막 체결가(표기 ≤ "0904" 마지막 봉 종가) > E이면 표기 ≥ "0905" 첫 봉 시가에 산다.
- R2: H = max(E, 09:01~09:04 고가). 09:05~09:29 봉 중 처음 고가 > H인 봉에서 max(H + 1호가, 그 봉 시가)에 산다.
- R3: E에 산다. 09:01부터 T = E·1.03(호가 올림), S = E·0.98. 봉 시가 ≤ S면 시가에서, 저가 ≤ S면 S에서 판다. 둘 다 닿은 뒤
  나가는 주문이라 호가에 맞춰 내린 값의 1호가 아래에 체결된다고 본다(같은 봉에서 고가 > T여도 손절). 시가 ≥ T면 T,
  고가 > T면 T에 판다(지정가 익절은 목표가보다 비싸게 팔렸다고 치지 않는다).
- R1·R2의 봉 시가 체결은 매수호가 쪽 체결일 수 있어 최대 1호가 유리하다(탐색에서 +1호가로 본다). D1'은 신호의
  정보와 함께 "산 쪽이 더 유동적인" 구성 차이를 담는다(동률은 사지 않으므로).
- 비용 0.30%.

**질문과 판정(`judge`).** 관측 = 진입일 하루, 규칙이 산 종목일의 (수익률 - 비용) 평균. E1·E2·E3(비용 뒤 > 0), D1'(R1이
산 종목일 - 그날 모든 종목일을 같은 방식으로 09:05 첫 봉 시가에 샀을 때), D3(R3 - R0). 연구 구간 20일 이상, 평균 > 0,
t ≥ 2.5, 앞·뒤 절반 > 0, 홀드아웃(마지막 15진입일) > 0. 후보: R1은 E1과 D1', R2는 E2, R3는 E3와 D3.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

STUDY_VERSION = 1
COST = 0.003
HOUR_END = "1000"
OPEN = "0900"
CONFIRM = "0904"
AFTER_CONFIRM = "0905"
BREAKOUT_LAST = "0929"
STOP = 0.02
TAKE = 0.03
HOLDOUT_DAYS = 15
MIN_DAYS = 20
MIN_T = 2.5

Bar = tuple[str, float, float, float, float]

_TICKS = (
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
)


def tick(price: float) -> float:
    """KRX 호가 단위(2023년 개편, 코스피·코스닥 공통)."""
    for bound, step in _TICKS:
        if price < bound:
            return float(step)
    return 1_000.0


def round_up(price: float) -> float:
    step = tick(price)
    return math.ceil(price / step - 1e-9) * step


def round_down(price: float) -> float:
    step = tick(price)
    return math.floor(price / step + 1e-9) * step


def _sell_below(price: float) -> float:
    """닿은 뒤 나가는 매도: 호가에 맞춰 내린 가격에서 1호가 아래."""
    p = round_down(price)
    return p - tick(p)


def hour(bars: Sequence[Bar]) -> list[Bar]:
    """10:00 전 봉만, 표기 순. 가격이 유한하고 양수인 봉만."""
    out = [b for b in bars if b[0] < HOUR_END and all(math.isfinite(x) and x > 0 for x in b[1:])]
    return sorted(out, key=lambda b: b[0])


def _exit_after(bars: Sequence[Bar], index: int) -> float:
    """진입한 봉(`index`) 뒤 마지막 봉의 종가. 뒤에 봉이 없으면 진입한 봉의 종가."""
    return bars[-1][4] if index < len(bars) - 1 else bars[index][4]


def r0(bars: Sequence[Bar]) -> float | None:
    b = hour(bars)
    if not b or b[0][0] != OPEN:
        return None
    return b[-1][4] / b[0][1] - 1


def buy_at_0905(
    bars: Sequence[Bar], *, after: str = AFTER_CONFIRM, entry_ticks: int = 0
) -> float | None:
    """조건 없이 표기 ≥ `after` 첫 봉 시가에 산다(D1'의 비교 대상이자 R1의 체결 방식).
    `entry_ticks`는 탐색용: 시가보다 그만큼 호가 위에서 샀다고 친다(시장가 매수는 매도호가를 낸다)."""
    b = hour(bars)
    if not b or b[0][0] != OPEN:
        return None
    for i, bar in enumerate(b):
        if bar[0] >= after:
            fill = bar[1] + entry_ticks * tick(bar[1])
            return _exit_after(b, i) / fill - 1
    return None


def r1(
    bars: Sequence[Bar], *, confirm: str = CONFIRM, after: str = AFTER_CONFIRM, entry_ticks: int = 0
) -> float | None:
    b = hour(bars)
    if not b or b[0][0] != OPEN:
        return None
    entry0 = b[0][1]
    decided = [x for x in b if x[0] <= confirm]
    if decided[-1][4] <= entry0:
        return None
    return buy_at_0905(b, after=after, entry_ticks=entry_ticks)


def first_trade_as_open(bars: Sequence[Bar]) -> list[Bar]:
    """탐색용: 09:00 봉이 없는 종목일의 첫 체결 봉을 09:00 봉으로 본다(개장 VI로 첫 체결이 늦은 종목)."""
    b = hour(bars)
    if not b or b[0][0] == OPEN:
        return b
    return [(OPEN, *b[0][1:]), *b[1:]]


def r2(bars: Sequence[Bar], *, entry_ticks: int = 0) -> float | None:
    b = hour(bars)
    if not b or b[0][0] != OPEN:
        return None
    h = max([b[0][1], *(x[2] for x in b if "0901" <= x[0] <= CONFIRM)])
    for i, bar in enumerate(b):
        if not AFTER_CONFIRM <= bar[0] <= BREAKOUT_LAST:
            continue
        if bar[2] > h:
            fill = max(h + tick(h), bar[1] + entry_ticks * tick(bar[1]))
            return _exit_after(b, i) / fill - 1
    return None


def r3(bars: Sequence[Bar], *, stop: float = STOP, take: float = TAKE) -> float | None:
    b = hour(bars)
    if not b or b[0][0] != OPEN:
        return None
    e = b[0][1]
    target = round_up(e * (1 + take))
    floor = e * (1 - stop)
    for bar in b[1:]:
        o, hi, lo = bar[1], bar[2], bar[3]
        if o <= floor:
            return _sell_below(o) / e - 1
        if o >= target:
            return target / e - 1
        if lo <= floor:
            return _sell_below(floor) / e - 1
        if hi > target:
            return target / e - 1
    return b[-1][4] / e - 1


# --- 관측과 판정 ---------------------------------------------------------------------------


def day_values(
    days: Mapping[date, Sequence[Sequence[Bar]]],
    rule: Callable[[Sequence[Bar]], float | None],
    *,
    minus: Callable[[Sequence[Bar]], float | None] | None = None,
    against_all: Callable[[Sequence[Bar]], float | None] | None = None,
    cost: float = COST,
) -> dict[date, float]:
    """하루 값. 기본은 규칙이 산 종목일의 (수익률 - 비용) 평균.

    `minus`: 같은 종목일의 다른 규칙 값을 뺀 차이(D3). `against_all`: 그날 모든 종목일의 그 값 평균을 뺀 차이(D1').
    차이 질문에는 비용을 빼지 않는다(양쪽 모두 한 번씩 사고판다).
    """
    out: dict[date, float] = {}
    for d, names in days.items():
        vals = []
        for bars in names:
            v = rule(bars)
            if v is None:
                continue
            if minus is not None:
                m = minus(bars)
                if m is None:
                    continue
                vals.append(v - m)
            else:
                vals.append(v if against_all is not None else v - cost)
        if not vals:
            continue
        value = statistics.fmean(vals)
        if against_all is not None:
            base = [x for bars in names if (x := against_all(bars)) is not None]
            if not base:
                continue
            value -= statistics.fmean(base)
        out[d] = value
    return out


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


def judge_one(
    key: str, text: str, values: Mapping[date, float], entry_days: Sequence[date]
) -> Verdict:
    """연구 구간 = 진입일 중 마지막 15일 전. 절반은 연구 구간 관측을 날짜순으로 반."""
    held = set(sorted(entry_days)[-HOLDOUT_DAYS:])
    study = [values[d] for d in sorted(values) if d not in held]
    hold = [values[d] for d in sorted(values) if d in held]
    m, t = mean_t(study)
    half = len(study) // 2
    halves = (mean_t(study[:half])[0], mean_t(study[half:])[0])
    hm = mean_t(hold)[0]
    if len(study) < MIN_DAYS or not hold:
        state = "not enough days"
    elif (
        m is not None
        and t is not None
        and m > 0
        and t >= MIN_T
        and all(h is not None and h > 0 for h in halves)
        and hm is not None
        and hm > 0
    ):
        state = "established"
    else:
        state = "not established"
    return Verdict(key, text, len(study), m, t, halves, len(hold), hm, state)


QUESTIONS = (
    ("E1", "R1(5분 확인 뒤 09:05 매수)이 비용 0.30% 뒤에 남는다"),
    ("D1'", "R1의 확인 신호가 같은 시각 조건 없는 매수보다 낫다"),
    ("E2", "R2(시초 5분 고가 돌파 매수)가 비용 0.30% 뒤에 남는다"),
    ("E3", "R3(손절 -2%·익절 +3%)이 비용 0.30% 뒤에 남는다"),
    ("D3", "R3의 손절·익절이 그냥 10시 전까지 들고 있는 것보다 낫다"),
)


def judge(days: Mapping[date, Sequence[Sequence[Bar]]], *, cost: float = COST) -> list[Verdict]:
    entry_days = sorted(days)
    values = {
        "E1": day_values(days, r1, cost=cost),
        "D1'": day_values(days, r1, against_all=buy_at_0905, cost=cost),
        "E2": day_values(days, r2, cost=cost),
        "E3": day_values(days, r3, cost=cost),
        "D3": day_values(days, r3, minus=r0, cost=cost),
    }
    return [judge_one(k, text, values[k], entry_days) for k, text in QUESTIONS]


def candidates(verdicts: Sequence[Verdict]) -> list[str]:
    ok = {v.key for v in verdicts if v.state == "established"}
    out = []
    if {"E1", "D1'"} <= ok:
        out.append("R1")
    if "E2" in ok:
        out.append("R2")
    if {"E3", "D3"} <= ok:
        out.append("R3")
    return out
