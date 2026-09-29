"""전일 상한가 판정(표시·사후 기록 전용). 순수.

상한가 = 기준가격(전일 종가) + 기준가격 곱하기 30%를 기준가격 구간 호가단위로 내린 것, 그 합을 결과 가격 구간 호가단위로
한 번 더 내린 것. 기준가격이 호가단위에 맞는 정수면 "기준가격 곱하기 1.3을 원 단위로 내린 뒤 결과 가격 구간 호가단위로
내림"과 같다(호가단위가 서로 배수라서). 호가단위는 2023-01-25부터 KOSPI·KOSDAQ가 같다.

판정은 `>=`로 한다. yfinance 봉 일부는 두 번째 내림 전 값(예: 기준 4,020 → 5,225, 규정상 5,220)에서 멈춰 있다.

기준가격이 전일 종가가 아닌 날(권리락·배당락·액면 변경·신규 상장·거래정지 해제), 가격제한이 없는 날(정리매매),
호가단위가 다른 ETF는 틀릴 수 있다. 조정된 과거 봉(소수점·호가단위에 안 맞는 종가)은 판정하지 않는다.
목록 선정·채점에는 쓰지 않는다.
"""

from __future__ import annotations

from decimal import Decimal

LOCKED = "LOCKED"
"""점상: 시가·고가·저가·종가가 모두 상한가."""
CLOSED = "CLOSED"
"""상한가 마감, 장중 거래가 있었다(상한가 아래에서 거래된 적이 있다)."""
TOUCHED = "TOUCHED"
"""장중 상한가에 닿았으나 그 아래에서 마감."""

_TICKS = ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50), (200_000, 100), (500_000, 500))


def tick(price: int) -> int:
    for bound, size in _TICKS:
        if price < bound:
            return size
    return 1_000


def upper_limit(base: Decimal) -> int | None:
    """기준가격의 상한가. 기준가격이 조정된 값(정수가 아니거나 호가단위에 안 맞음)이면 None."""
    if base <= 0 or base != base.to_integral_value():
        return None
    b = int(base)
    if b % tick(b):
        return None
    raw = b * 13 // 10
    size = tick(raw)
    return raw // size * size


def state(o: Decimal, h: Decimal, l: Decimal, c: Decimal, base: Decimal) -> str | None:  # noqa: E741
    limit = upper_limit(base)
    if limit is None:
        return None
    if min(o, h, l, c) >= limit:
        return LOCKED
    if c >= limit:
        return CLOSED
    if h >= limit:
        return TOUCHED
    return None
