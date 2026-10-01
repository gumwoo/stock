"""09:00 시가에 산 뒤 첫 1시간의 길(소유자 단타 기준). 순수.

진입가는 09:00 봉의 시가다(09:00 봉이 없으면 잴 수 없다 — 시초가에 잠긴 종목 등). 팔 수 있는 봉은 09:01~09:59다
(공시 연구 `disclosure_first_hour.measure`와 같은 규칙: 09:00 봉의 고가는 시초 체결 직후의 값이라 그 가격에 팔았다고 보지 않는다).
목표 도달은 처음으로 고가가 진입가 곱하기 (1 + 목표)에 닿은 봉이다. "닿기 전 최저"는 09:00 봉부터 그 봉까지(둘 다 포함)의
저가 최솟값이다 — 시가에 산 사람은 09:00 봉 안의 하락도 겪고, 1분봉 안의 순서는 모르므로 같은 봉에서 목표와 최저가
함께 나오면 최저가 먼저였다고 본다(보수적). 공시 연구의 MAE(09:01부터)보다 깊게 나올 수 있다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

TARGETS = (0.025, 0.05)


@dataclass(frozen=True, slots=True)
class Bar:
    minute: int
    """09:00부터 지난 분(09:00 봉 = 0)."""
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class EarlyPath:
    entry: float
    hit_25: int | None
    """+2.5%에 처음 닿은 분(09:01=1). 못 닿으면 None."""
    hit_5: int | None
    low_before_25: float
    """09:00 봉부터 +2.5%에 닿은 봉까지(닿지 못했으면 09:59까지)의 최저, 진입가 대비 %."""
    at_10: float
    """10:00 전 마지막 봉 종가, 진입가 대비 %."""

    def hit_25_within(self, minutes: int) -> bool:
        return self.hit_25 is not None and self.hit_25 < minutes


def measure(bars: Sequence[Bar]) -> EarlyPath | None:
    hour = sorted((b for b in bars if 0 <= b.minute < 60), key=lambda b: b.minute)
    if not hour or hour[0].minute != 0 or hour[0].open <= 0:
        return None
    entry = hour[0].open
    sellable = [b for b in hour if b.minute >= 1]
    if not sellable:
        return None

    def first_hit(target: float) -> int | None:
        line = entry * (1 + target)
        return next((b.minute for b in sellable if b.high >= line), None)

    hit_25 = first_hit(0.025)
    upto = [b for b in hour if hit_25 is None or b.minute <= hit_25]
    return EarlyPath(
        entry=entry,
        hit_25=hit_25,
        hit_5=first_hit(0.05),
        low_before_25=(min(b.low for b in upto) / entry - 1) * 100,
        at_10=(sellable[-1].close / entry - 1) * 100,
    )
