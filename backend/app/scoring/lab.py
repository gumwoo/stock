"""전략 실험실: 매일 쌓이는 아침 목록 기록으로 규칙과 종목 조건을 비교하고, 미리 고정한 가설을 앞으로 잰다. 순수.

판단 보조다(사라·팔라를 말하지 않는다). 숫자는 모두 지난 기록이다.

**체결과 비용은 기존 연구와 같다.** 9시 시가(09:00 봉 시가)에 사고, 손절·익절은 `entry_rules.r3`의 체결 모델(09:01부터,
목표가 호가 올림·목표가 체결, 손절은 1호가 아래 체결, 봉 시가가 이미 넘었으면 시가)로 판다. 둘 다 안 닿으면 10시 전 마지막
봉 종가. 비용은 `entry_rules.COST`(0.30%). "+X% 도달"도 같은 판정(`entry_rules.reached`)이다 — 카톡·오늘의 관찰의 "보통"
(`early_path`, 고가 ≥ 목표)과 정의가 조금 다르다.

**관측은 날짜다.** 같은 날 종목들은 장 분위기로 함께 움직여서, 종목일 하나하나를 관측으로 세면 불확실성이 작게 보인다. 그날
해당 종목들의 평균 하나가 관측 하나이고, t와 앞·뒤 절반도 날짜로 센다(`intraday_review`와 같다).

**비교표는 탐색이다.** 칸이 수백 개라 2·SE 문턱이면 효과가 없어도 몇 칸은 켜진다. 그래서 비교표는 판정하지 않고 "표본
부족 / 앞뒤 같은 방향 / 앞뒤 갈림"만 붙인다. 판정은 아래에 미리 고정한 가설(S1·S2)에만 하고, 문턱은 아침 목록 가설 H1~H7과
같다: 고정 뒤 20일까지는 읽지 않고, 처음 60일로 한 번만 판정(평균이 예측 방향, |t| ≥ 2, 앞·뒤 절반 모두 예측 방향).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app.scoring import entry_rules
from app.scoring.entry_rules import Bar, mean_t

COST = entry_rules.COST
TAKES = (0.025, 0.03, 0.05)
STOPS: tuple[float | None, ...] = (None, 0.01, 0.015, 0.02, 0.03)
HEADLINE: tuple[tuple[float, float | None], ...] = ((0.025, None), (0.025, 0.02), (0.05, 0.01))
"""성적표와 비교표에 늘 보이는 규칙: +2.5%·손절 없음(10시), +2.5%·-2%, +5%·-1%."""

READ_DAYS = 20
DECIDE_DAYS = 60
MIN_T = 2.0

# 가설을 고정한 시각. 이 뒤에 얼린 목록 날(스냅숏 asof가 이보다 뒤)만 가설 판정에 센다.
FROZEN_AT = datetime(2026, 10, 2, 13, 30, tzinfo=UTC)  # 2026-10-02 22:30 KST(커밋 직전)


@dataclass(frozen=True, slots=True)
class Sample:
    """목록 날 하나의 종목 하나(또는 3개월 기준표의 종목·진입일 하나)."""

    day: date
    bars: tuple[Bar, ...]
    """09:00~09:59 1분봉, ("0901", 시가, 고가, 저가, 종가)."""
    market: str | None = None
    gap: float | None = None
    """09:00 봉 시가 / 전 거래일 종가 - 1(%)."""
    prev_change: float | None = None
    """전 거래일 종가 등락(%)."""
    reasons: tuple[str, ...] = ()
    technical: float | None = None
    fundamental: float | None = None
    total: float | None = None
    rank: int | None = None
    volume_ratio: float | None = None
    """전 거래일 거래량 / 그 앞 20세션 평균(기준표만)."""
    price: float | None = None
    direction: str | None = None
    """공시 방향(기준표만): 좋음 / 나쁨 / 애매."""
    excluded: str | None = None
    """선정 3에서 목록에서 뺀 이유(LOW_SCORE / PREV_SURGE). 남은 종목·선정 2 날은 None."""
    screened: bool = False
    """선정 3(제외 규칙을 적용한) 목록 날인가. 선정 2 날은 "남음/뺌"으로 나눌 수 없다."""


def ret(bars: Sequence[Bar], take: float, stop: float | None) -> float | None:
    """규칙 수익률(%, 비용 뒤). 09:00 봉이 없으면 None."""
    v = entry_rules.r3(bars, stop=stop, take=take)
    return None if v is None else (v - COST) * 100


def at_ten(bars: Sequence[Bar]) -> float | None:
    """9시 시가 → 10시 전 마지막 종가(%, 비용 전)."""
    v = entry_rules.r0(bars)
    return None if v is None else v * 100


def locked(bars: Sequence[Bar]) -> bool:
    """09:30 전 모든 봉이 한 가격(시초 상한가 잠김 등): 시가에 실제로 살 수 없었다."""
    early = [b for b in bars if b[0] < "0930"]
    return bool(early) and len({x for b in early for x in b[1:]}) == 1


# --- 구간 ---------------------------------------------------------------------------------------------------


def _cut(v: float | None, edges: Sequence[float], labels: Sequence[str]) -> str | None:
    if v is None:
        return None
    for e, label in zip(edges, labels, strict=False):
        if v < e:
            return label
    return labels[-1]


GAP_LABELS = ("-3% 미만", "-3~-1%", "-1~+1%", "+1~+3%", "+3% 이상")
PREV_LABELS = ("-5% 미만", "-5~-2%", "-2~0%", "0~+2%", "+2~+5%", "+5~+15%", "+15% 이상")
SCORE_LABELS = ("40 미만", "40~60", "60~75", "75 이상")
RANK_LABELS = ("1~10위", "11~20위", "21~30위", "31~40위")
VOLUME_LABELS = ("0.7배 미만", "0.7~1.5배", "1.5~3배", "3~10배", "10배 이상")
PRICE_LABELS = ("2천 미만", "2천~5천", "5천~1만", "1만~5만", "5만 이상")
MARKET_GAP_LABELS = ("갭 -1% 미만", "갭 ±1%", "갭 +1% 이상")
EXCLUDED_LABEL = {
    "LOW_SCORE": "뺌: 판단 점수 40 미만",
    "PREV_SURGE": "뺌: 전일 +15% 이상",
    "GAP_UP": "뺌: 예상 갭 +3% 이상(08:50)",
}
_ORDER = {
    label: i
    for i, label in enumerate(
        (
            "KOSPI",
            "KOSDAQ",
            *GAP_LABELS,
            *PREV_LABELS,
            *SCORE_LABELS,
            "점수 없음",
            "목록에 남음",
            *EXCLUDED_LABEL.values(),
            *RANK_LABELS,
            *VOLUME_LABELS,
            *PRICE_LABELS,
            "좋음",
            "나쁨",
            "애매",
            *(f"{m} {g}" for m in ("KOSPI", "KOSDAQ") for g in MARKET_GAP_LABELS),
        )
    )
}
"""구간이 있는 조건은 작은 값부터, 범주(목록 이유 등)는 이름순."""


def gap_bucket(v: float | None) -> str | None:
    return _cut(v, (-3, -1, 1, 3), GAP_LABELS)


def prev_bucket(v: float | None) -> str | None:
    return _cut(v, (-5, -2, 0, 2, 5, 15), PREV_LABELS)


def score_bucket(v: float | None) -> str:
    return _cut(v, (40, 60, 75), SCORE_LABELS) or "점수 없음"


def rank_bucket(v: int | None) -> str | None:
    return _cut(v, (11, 21, 31), RANK_LABELS)


def volume_bucket(v: float | None) -> str | None:
    return _cut(v, (0.7, 1.5, 3, 10), VOLUME_LABELS)


def price_bucket(v: float | None) -> str | None:
    return _cut(v, (2000, 5000, 10000, 50000), PRICE_LABELS)


def market_gap(s: Sample) -> str | None:
    g = _cut(s.gap, (-1, 1), MARKET_GAP_LABELS)
    return None if s.market is None or g is None else f"{s.market} {g}"


Feature = Callable[[Sample], Iterable[str] | str | None]


def excluded_bucket(s: Sample) -> tuple[str, ...]:
    """선정 3 날만 나눈다(그 전 날은 규칙이 없어 모두 "남음"으로 보이면 다른 기간끼리 비교가 된다)."""
    if not s.screened:
        return ()
    if not s.excluded:
        return ("목록에 남음",)
    return tuple(EXCLUDED_LABEL.get(x, x) for x in s.excluded.split(","))


OUR_FEATURES: tuple[tuple[str, Feature], ...] = (
    ("목록 제외", excluded_bucket),
    ("시장", lambda s: s.market),
    ("목록 이유", lambda s: s.reasons),
    ("기술 점수", lambda s: score_bucket(s.technical)),
    ("재무 점수", lambda s: score_bucket(s.fundamental)),
    ("합계 점수", lambda s: score_bucket(s.total)),
    ("목록 순위", lambda s: rank_bucket(s.rank)),
    ("시가 갭", lambda s: gap_bucket(s.gap)),
    ("전일 등락", lambda s: prev_bucket(s.prev_change)),
    ("시장·갭", market_gap),
)
REFERENCE_FEATURES: tuple[tuple[str, Feature], ...] = (
    ("시장", lambda s: s.market),
    ("시가 갭", lambda s: gap_bucket(s.gap)),
    ("전일 등락", lambda s: prev_bucket(s.prev_change)),
    ("전일 거래량", lambda s: volume_bucket(s.volume_ratio)),
    ("주가", lambda s: price_bucket(s.price)),
    ("공시 방향", lambda s: s.direction),
    ("시장·갭", market_gap),
)


# --- 날짜 기준 집계 -------------------------------------------------------------------------------------------


def daily(samples: Iterable[Sample], value: Callable[[Sample], float | None]) -> dict[date, float]:
    """그날 값이 있는 종목들의 평균. 날짜 하나가 관측 하나."""
    by: dict[date, list[float]] = defaultdict(list)
    for s in samples:
        v = value(s)
        if v is not None:
            by[s.day].append(v)
    return {d: sum(v) / len(v) for d, v in by.items()}


@dataclass(frozen=True, slots=True)
class RuleStat:
    take: float
    stop: float | None
    n: int
    """종목일 수."""
    days: int
    mean: float | None
    """날짜별 평균의 평균(%, 비용 뒤)."""
    t: float | None
    first: float | None
    second: float | None
    flag: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "take": self.take,
            "stop": self.stop,
            "n": self.n,
            "days": self.days,
            "mean": self.mean,
            "t": self.t,
            "first": self.first,
            "second": self.second,
            "flag": self.flag,
        }


def flag(days: int, first: float | None, second: float | None, *, min_days: int = READ_DAYS) -> str:
    """탐색 표시: 판정이 아니다."""
    if days < min_days or first is None or second is None:
        return "표본 부족"
    if (first > 0) == (second > 0):
        return "앞뒤 같은 방향"
    return "앞뒤 갈림"


def rule_stat(samples: Sequence[Sample], take: float, stop: float | None) -> RuleStat:
    by = daily(samples, lambda s: ret(s.bars, take, stop))
    ordered = [by[d] for d in sorted(by)]
    m, t = mean_t(ordered)
    half = len(ordered) // 2
    first = mean_t(ordered[:half])[0] if half else None
    second = mean_t(ordered[half:])[0] if half else None
    n = sum(1 for s in samples if ret(s.bars, take, stop) is not None)
    return RuleStat(
        take, stop, n, len(ordered), m, t, first, second, flag(len(ordered), first, second)
    )


def hit_rate(samples: Sequence[Sample], take: float, *, within: str = "1000") -> float | None:
    """종목일 중 `take`에 `within` 표기 전에 닿은 비율(손절 없음)."""
    got = [
        entry_rules.reached(s.bars, take=take) for s in samples if s.bars and s.bars[0][0] == "0900"
    ]
    if not got:
        return None
    return sum(1 for x in got if x is not None and x < within) / len(got)


def grid(samples: Sequence[Sample]) -> list[dict[str, Any]]:
    out = []
    for market in (None, "KOSPI", "KOSDAQ"):
        pick = [s for s in samples if market is None or s.market == market]
        for take in TAKES:
            for stop in STOPS:
                out.append({"market": market or "전체", **rule_stat(pick, take, stop).as_dict()})
    return out


def conditions(
    samples: Sequence[Sample], features: Sequence[tuple[str, Feature]]
) -> list[dict[str, Any]]:
    out = []
    for name, feature in features:
        groups: dict[str, list[Sample]] = defaultdict(list)
        for s in samples:
            v = feature(s)
            for label in [v] if isinstance(v, str) or v is None else v:
                if label is not None:
                    groups[label].append(s)
        for label, pick in sorted(
            groups.items(), key=lambda kv: (_ORDER.get(kv[0], len(_ORDER)), kv[0])
        ):
            out.append(
                {
                    "feature": name,
                    "label": label,
                    "n": len(pick),
                    "hit25": hit_rate(pick, 0.025),
                    "rules": [rule_stat(pick, t, st).as_dict() for t, st in HEADLINE],
                }
            )
    return out


# --- 고정 가설 ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hypothesis:
    key: str
    text: str
    select: Callable[[Sample], bool]
    take: float
    stop: float | None
    sign: int
    """+1: 비용 뒤 평균이 0보다 크다고 예측, -1: 작다고 예측."""
    basis: str
    """왜 이 가설을 골랐나(고정 전 근거)."""


HYPOTHESES: tuple[Hypothesis, ...] = (
    Hypothesis(
        "S1",
        "시가 갭 -1% 미만 종목을 9시 시가에 사서 +5% 익절·-1% 손절이면 비용 뒤 평균이 0보다 크다",
        lambda s: s.gap is not None and s.gap < -1,
        0.05,
        0.01,
        1,
        "소유자가 본 장면(10/1 한화솔루션: 갭다운 뒤 반등)과, 비용 0.2%·손절선 그대로 체결로 잡은 첫 탐색에서 나왔다. "
        "기존 연구와 같은 체결 모델·비용 0.30%의 3개월 기준표에서는 음수라 근거는 약하다 — 앞으로의 우리 목록으로 다시 묻는다.",
    ),
    Hypothesis(
        "S2",
        "시가 갭 +1% 이상 종목을 사서 +2.5% 익절·손절 없음(10시 매도)이면 비용 뒤 평균이 0보다 작다",
        lambda s: s.gap is not None and s.gap >= 1,
        0.025,
        None,
        -1,
        "3개월 기준표(57일)에서 갭업 종목은 어느 규칙이든 음수였고 앞·뒤 절반도 같은 방향이었다. 우리 목록 5일도 음수.",
    ),
)


@dataclass(frozen=True, slots=True)
class Judged:
    days: int
    n: int
    mean: float | None
    t: float | None
    first: float | None
    second: float | None
    state: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "n": self.n,
            "mean": self.mean,
            "t": self.t,
            "first": self.first,
            "second": self.second,
            "state": self.state,
        }


def judge(h: Hypothesis, samples: Sequence[Sample]) -> Judged:
    """고정 뒤 목록 날들의 가설 값. 처음 60일로 한 번만 판정(그 뒤는 다시 묻지 않는다)."""
    pick = [s for s in samples if h.select(s)]
    by = daily(pick, lambda s: ret(s.bars, h.take, h.stop))
    first_days = sorted(by)[:DECIDE_DAYS]
    ordered = [by[d] for d in first_days]
    m, t = mean_t(ordered)
    half = len(ordered) // 2
    a = mean_t(ordered[:half])[0] if half else None
    b = mean_t(ordered[half:])[0] if half else None
    counted = set(first_days)
    n = sum(1 for s in pick if s.day in counted and ret(s.bars, h.take, h.stop) is not None)
    if len(ordered) < READ_DAYS:
        state = f"기록 중 ({len(ordered)}/{READ_DAYS}일, 판정 아님)"
    elif len(ordered) < DECIDE_DAYS:
        state = f"읽는 중 ({len(ordered)}/{DECIDE_DAYS}일, 판정 아님)"
    elif (
        m is not None
        and t is not None
        and m * h.sign > 0
        and t * h.sign >= MIN_T
        and a is not None
        and b is not None
        and a * h.sign > 0
        and b * h.sign > 0
    ):
        state = "성립"
    else:
        state = "성립 안 함"
    return Judged(len(ordered), n, m, t, a, b, state)


@dataclass
class Exclusions:
    """날짜별로 계산에서 뺀 종목 수와 이유."""

    counts: dict[str, int] = field(default_factory=dict)

    def add(self, why: str) -> None:
        self.counts[why] = self.counts.get(why, 0) + 1
