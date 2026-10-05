"""9시 전 예상 시가 갭 판정(순수). 예상 시가가 전일 대비 +3% 이상이면 9시 시가 매수 목록에서 뺀다.

근거: 3개월 공시 표본에서 시가 갭 +3% 이상 종목을 9시 시가에 사서 +2.5% 익절·손절 없음(비용 0.30% 뒤)은 평균 -1.38%
(277건, t -3.9, 앞·뒤 절반 모두 마이너스), 우리 목록 5일도 매일 마이너스였다(소유자 결정, 2026-10-05).

KIS 응답(output2)의 `antc_cnpr`(예상 체결가)와 `stck_sdpr`(기준가)로 변화율을 직접 계산하고, KIS가 준 `antc_cntg_prdy_ctrt`와
0.15%p 넘게 다르면 판정하지 않는다(부호·단위가 예상과 다를 때 엉뚱한 종목을 빼지 않게). 예상 가격·기준가·예상 수량이
0 이하이거나 없으면(아직 체결될 호가가 없음) 역시 판정하지 않는다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from typing import Any

GAP_UP = "GAP_UP"
GAP_UP_AT = 3.0
MISMATCH_PP = 0.15
DECIDE_BY = time(8, 54)
"""08:50 판정을 반영하는 마지막 시각. 08:55 실시간 연결 전에 목록이 걸러져 있어야 한다."""


@dataclass(frozen=True, slots=True)
class Quote:
    expected_price: float | None
    base_price: float | None
    reported_pct: float | None
    volume: int | None
    mkop_code: str | None

    @property
    def change_pct(self) -> float | None:
        if not self.expected_price or not self.base_price or self.base_price <= 0:
            return None
        return (self.expected_price / self.base_price - 1) * 100

    @property
    def judgeable(self) -> bool:
        c = self.change_pct
        return (
            c is not None
            and (self.expected_price or 0) > 0
            and (self.volume or 0) > 0
            and self.reported_pct is not None
            and abs(c - self.reported_pct) <= MISMATCH_PP
        )

    @property
    def mismatched(self) -> bool:
        """값은 있는데 KIS의 대비율과 어긋난다."""
        c = self.change_pct
        return (
            c is not None
            and self.reported_pct is not None
            and abs(c - self.reported_pct) > MISMATCH_PP
        )


def _num(raw: Any) -> float | None:
    try:
        v = float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse(output2: Mapping[str, Any] | None) -> Quote | None:
    if not isinstance(output2, Mapping):
        return None
    vol = _num(output2.get("antc_vol"))
    code = output2.get("antc_mkop_cls_code")
    return Quote(
        expected_price=_num(output2.get("antc_cnpr")),
        base_price=_num(output2.get("stck_sdpr")),
        reported_pct=_num(output2.get("antc_cntg_prdy_ctrt")),
        volume=int(vol) if vol is not None else None,
        mkop_code=str(code)[:8] if code not in (None, "") else None,
    )


def gap_up(q: Quote) -> bool:
    c = q.change_pct
    return q.judgeable and c is not None and c >= GAP_UP_AT


def next_reason(current: str | None, is_gap_up: bool) -> str | None:
    """08:50 판정 뒤 목록 행의 제외 이유. 이미 다른 이유(점수·전일 급등)로 빠진 행은 건드리지 않는다."""
    if current not in (None, GAP_UP):
        return current
    return GAP_UP if is_gap_up else None
