"""증권사 투자의견 요약(화면 참고 표시·사후 기록 전용). 순수 — DB도 시계도 읽지 않는다.

목록 날 D에는 D보다 앞선 날짜의 리포트만 쓴다(날짜에 시각이 없어서, 공시와 같은 규칙). 창은 D 직전 90일.
증권사마다 창 안 최신 리포트 하나로 평균 목표주가와 의견 분포를 낸다. 목록 선정·채점에는 쓰지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

WINDOW_DAYS = 90

BUY = "BUY"
HOLD = "HOLD"
SELL = "SELL"
OTHER = "OTHER"

# 원문(소문자, 공백 제거) → 정규화. 첫 수집에서 본 값(BUY, 매수, Buy, Strong BUY, Outperform)과 흔한 표기.
_OPINIONS: dict[str, str] = {
    **dict.fromkeys(
        (
            "buy",
            "매수",
            "strongbuy",
            "적극매수",
            "outperform",
            "overweight",
            "tradingbuy",
            "단기매수",
            "비중확대",
            "accumulate",
            "add",
        ),
        BUY,
    ),
    **dict.fromkeys(
        (
            "hold",
            "중립",
            "neutral",
            "marketperform",
            "시장수익률",
            "시장평균",
            "보유",
            "marketweight",
            "equalweight",
            "sectorperform",
        ),
        HOLD,
    ),
    **dict.fromkeys(
        ("sell", "매도", "underperform", "underweight", "reduce", "비중축소", "strongsell"),
        SELL,
    ),
}


def normalize(opinion: str) -> str:
    return _OPINIONS.get("".join(opinion.lower().split()), OTHER)


@dataclass(frozen=True, slots=True)
class Report:
    id: int
    report_date: date
    broker: str
    opinion: str
    target_price: float | None


def summarize(
    reports: Sequence[Report],
    day: date,
    *,
    prev_close: float | None,
    truncated_before: date | None = None,
) -> dict[str, Any]:
    """목록 날 `day`의 요약. `truncated_before`가 있으면 그 날짜보다 앞은 받지 못했다(100행 상한).

    창 안에 리포트가 없으면 count 0(조회는 됐다는 뜻 — 조회하지 못한 경우는 부르는 쪽이 None으로 거른다).
    """
    start = day - timedelta(days=WINDOW_DAYS)
    ordered = sorted(reports, key=lambda r: (r.report_date, r.id))
    window = [r for r in ordered if start <= r.report_date < day]
    latest: dict[str, Report] = {}
    for r in window:
        latest[r.broker] = r  # 날짜·id 순이라 마지막이 최신

    raised = lowered = 0
    for broker, r in latest.items():
        if r.target_price is None:
            continue
        before = [
            x
            for x in ordered
            if x.broker == broker
            and x.target_price is not None
            and (x.report_date, x.id) < (r.report_date, r.id)
        ]
        if before:
            if r.target_price > before[-1].target_price:  # type: ignore[operator]
                raised += 1
            elif r.target_price < before[-1].target_price:  # type: ignore[operator]
                lowered += 1

    targets = [r.target_price for r in latest.values() if r.target_price is not None]
    avg = sum(targets) / len(targets) if targets else None
    dist = {BUY: 0, HOLD: 0, SELL: 0, OTHER: 0}
    for r in latest.values():
        dist[normalize(r.opinion)] += 1
    newest = window[-1] if window else None
    return {
        "window_days": WINDOW_DAYS,
        "count": len(window),
        "brokers": len(latest),
        "avg_target": round(avg) if avg is not None else None,
        "target_brokers": len(targets),
        "upside_pct": round((avg / prev_close - 1) * 100, 1) if avg and prev_close else None,
        "opinions": dist,
        "raised": raised,
        "lowered": lowered,
        "latest": None
        if newest is None
        else {
            "date": newest.report_date.isoformat(),
            "broker": newest.broker,
            "opinion": newest.opinion,
            "label": normalize(newest.opinion),
            "target": round(newest.target_price) if newest.target_price is not None else None,
        },
        # 잘린 조회: 창 시작보다 늦은 날짜부터만 받았으면 건수는 "이상"이다.
        "truncated": truncated_before is not None and truncated_before > start,
    }
