"""밤사이 미국 업종 신호일, NXT 08:00에 샀다면. 순수. NXT 가격(데이터)보다 먼저 커밋한다.

**질문.** 밤사이 미국 업종 연구(`overnight_study`, v1)는 신호가 다음 날 KRX 갭에는 반영되는 경향이 있지만 9시 시가
이후에는 남지 않았다고 봤다. 한국에서 가장 먼저 살 수 있는 시각은 넥스트레이드(NXT) 프리마켓 08:00이다. 8시에
사면 신호 덕에 남는 구간이 있는가.

**이미 본 것.** 신호일·지표·연동 목록·KRX 결과(홀드아웃 포함)를 모두 본 뒤에 이 질문을 정했다. NXT 가격은 확인
호출 2회 말고는 보지 않았다. 같은 종목일의 KRX 9시→10시 값은 v1에서 이미 봤으므로 N3의 새 정보는 사실상 8시→9시
부분이고, N2와 N3는 독립된 증거가 아니다. 같은 표본을 다른 진입 시각으로 다시 본 것이지 독립된 확인이 아니다.

**표본.** v1 신호일 33일(연구 27 / 홀드아웃 6)과 연동 목록을 그대로 쓴다. 비교군은 신호일마다 v1 유동성 통과
종목 중 다섯 목록 어디에도 없고 그날 KRX 09:00 봉과 전날 종가가 있는 종목을 sha256("티커|날짜") 순으로 30개
(`control_sample`). 홀드아웃은 v1과 같은 날.

**값(종목일 하나).**
- 진입가 P8 = 08:00~08:04 봉 중 거래량 > 0인 봉의 거래량가중 종가. 5분 거래대금 근사가 1,000만 원 미만이면 관측하지
  않는다(`entry`). NXT 프리마켓은 08:00부터 단일가 없이 접속매매라 첫 체결이 1주일 수 있다.
- g8 = P8/전날 KRX 종가 - 1, g9 = KRX 9시 시가/전날 종가 - 1, a = KRX 9시 시가/P8 - 1, b = KRX 09:00 봉 종가/P8 - 1.
- |g8|·|g9| > 30.1%는 데이터 흔적, |g8| ≥ 29.5%는 상·하한가라 뺀다(`name_day`).
- 지표는 그날 관측 가능한 목록 종목이 2개 이상일 때만, 비교군은 5개 이상일 때만 값이 있다.

**질문.** 관측 = 신호일 하나, 지표별 목록 평균을 등가중(v1과 같음).
- N1 a - a_비교군 > 0, N2 a - 0.30% > 0, N3 b - 0.30% > 0, N4 b - b_비교군 > 0.
판정은 v1 `judge`와 같다. **포워드 기록 후보는 (N1과 N2) 또는 (N3과 N4)가 모두 성립할 때만**(`candidate`). 절대값만
성립하면 신호에 귀속할 수 없다. 후보여도 운영 규칙은 소유자가 정한다. 후보로 가는 길이 둘이라 우연 성립
가능성이 조금 늘지만, N2와 N3가 강하게 겹쳐 영향은 작다.

**절차.** 수집 뒤 판정 전에 관측 가능 수만 먼저 센다(`overnight-nxt --counts-only`). 탐색(판정 아님): 같은 종목일의
g8·g9-g8·KRX 9시→10시, 진입가(첫 봉 시가, 08:49 종가), 거래대금 문턱(0원, 5,000만 원), 지표 최소 1종목, 비용
0.20/0.40%, 종목일 모음의 중앙값·10% 절단평균, 신호 쪽·비교군 08:00~08:04 거래대금 중앙값, 관측 불가 이유.
"""

from __future__ import annotations

import hashlib
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from app.scoring import overnight_study as base

STUDY_VERSION = 1
ENTRY_FIRST = "0800"
ENTRY_LAST = "0804"
PRE_LAST = "0849"
MIN_VALUE = 10_000_000  # 원, 08:00~08:04 거래대금 근사
MIN_NAMES = 2
CONTROL_SIZE = 30
CONTROL_MIN = 5
LIMIT = 0.295
COST = base.COST

QUESTIONS = (
    ("N1", "8시에 산 신호 종목이 KRX 9시 시가까지 비교군보다 더 오른다"),
    ("N2", "8시에 사서 KRX 9시 시가에 팔면 비용 0.30% 뒤에 남는다"),
    ("N3", "8시에 사서 10시 직전에 팔면 비용 0.30% 뒤에 남는다"),
    ("N4", "8시에 산 신호 종목이 10시 직전까지 비교군보다 더 오른다"),
)

EXPLORE_KEYS = (
    ("g8", "8시 가격 - 전날 KRX 종가"),
    ("g9-g8", "갭 중 8시 뒤 9시 시가까지 생긴 부분"),
    ("krx", "KRX 9시 시가 → 10시 직전 - 비용(v1 O3와 같은 값)"),
)
"""판정하지 않는 값. 같은 관측일에 `judge`로 평균·t만 본다."""

Bar = tuple[str, float, float, float, float, float]
"""(시각 "HHMM", 시가, 고가, 저가, 종가, 거래량). "0800"은 08:00:00~08:00:59."""


def _early(bars: Sequence[Bar]) -> list[Bar]:
    return [b for b in bars if ENTRY_FIRST <= b[0] <= ENTRY_LAST and b[5] > 0]


def early_value(bars: Sequence[Bar]) -> float:
    """08:00~08:04 거래대금 근사 Σ(종가·거래량)."""
    return sum(b[4] * b[5] for b in _early(bars))


def entry(bars: Sequence[Bar], *, min_value: float = MIN_VALUE) -> float | None:
    """08:00~08:04 거래량가중 종가. 체결이 없거나 대금이 모자라면 None."""
    win = _early(bars)
    volume = sum(b[5] for b in win)
    value = early_value(bars)
    if not volume or value < min_value:
        return None
    return value / volume


def first_open(bars: Sequence[Bar]) -> float | None:
    """민감도: 08:00~08:04 첫 체결 봉의 시가(대금 조건 없음)."""
    win = sorted(_early(bars))
    return win[0][1] if win else None


def pre_close(bars: Sequence[Bar]) -> float | None:
    """민감도: 프리마켓 마지막(08:49 이하) 체결 봉의 종가."""
    win = sorted(b for b in bars if ENTRY_FIRST <= b[0] <= PRE_LAST and b[5] > 0)
    return win[-1][4] if win else None


@dataclass(frozen=True, slots=True)
class NameDay:
    g8: float
    g9: float
    a: float
    b: float
    krx: float
    """KRX 9시 시가 → 10시 직전(v1의 첫 1시간). 종목 구성 효과를 떼어 보는 탐색용."""


def name_day(p8: float | None, prev_close: float, open9: float, close10: float) -> NameDay | None:
    if p8 is None or min(p8, prev_close, open9, close10) <= 0:
        return None
    g8, g9 = p8 / prev_close - 1, open9 / prev_close - 1
    edge = base.PRICE_LIMIT + base.LIMIT_SLACK
    if abs(g8) > edge or abs(g9) > edge or abs(g8) >= LIMIT:
        return None
    return NameDay(g8, g9, open9 / p8 - 1, close10 / p8 - 1, close10 / open9 - 1)


def control_sample(
    day: date, candidates: Sequence[str], exclude: set[str], size: int = CONTROL_SIZE
) -> list[str]:
    """목록 밖 후보를 sha256("티커|날짜") 순으로. 고르는 사람이 없다."""
    pool = [t for t in candidates if t not in exclude]
    key = day.isoformat()
    return sorted(pool, key=lambda t: hashlib.sha256(f"{t}|{key}".encode()).hexdigest())[:size]


def observe(
    day: date,
    active: Sequence[str],
    lists: Mapping[str, Sequence[str]],
    values: Mapping[str, NameDay],
    control: Sequence[str],
    *,
    min_names: int = MIN_NAMES,
    cost: float = COST,
) -> base.Observation | None:
    """그날의 관측. `values`는 그날 관측 가능한 종목일(신호·비교군 모두)."""
    ctrl = [values[t] for t in control if t in values]
    ca = statistics.fmean(v.a for v in ctrl) if len(ctrl) >= CONTROL_MIN else None
    cb = statistics.fmean(v.b for v in ctrl) if len(ctrl) >= CONTROL_MIN else None
    obs = base.Observation(day, tuple(active))
    for ind in active:
        members = [values[n] for n in lists[ind] if n in values]
        if len(members) < min_names:
            continue
        a = statistics.fmean(v.a for v in members)
        b = statistics.fmean(v.b for v in members)
        per = {
            "N2": a - cost,
            "N3": b - cost,
            "g8": statistics.fmean(v.g8 for v in members),
            "g9-g8": statistics.fmean(v.g9 - v.g8 for v in members),
            "krx": statistics.fmean(v.krx for v in members) - cost,
        }
        if ca is not None and cb is not None:
            per["N1"] = a - ca
            per["N4"] = b - cb
        obs.by_indicator[ind] = per
    if not obs.by_indicator:
        return None
    for key, _ in QUESTIONS:
        vals = [p[key] for p in obs.by_indicator.values() if key in p]
        obs.values[key] = statistics.fmean(vals) if vals else None
    return obs


def candidate(verdicts: Sequence[base.Verdict]) -> bool:
    """포워드 기록 후보: 절대값과 비교군 대비가 짝으로 성립할 때만."""
    ok = {v.key for v in verdicts if v.state == "established"}
    return {"N1", "N2"} <= ok or {"N3", "N4"} <= ok
