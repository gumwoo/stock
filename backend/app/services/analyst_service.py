"""목록 날 기준 증권사 투자의견 요약(화면 참고 표시·사후 기록 전용). 읽기만 한다.

조회 기록이 창을 덮지 않는 종목은 결과에서 빠진다(화면은 줄을 그리지 않는다). 조회는 됐는데 리포트가 없으면 count 0.
리포트 날짜에 시각이 없어 목록 날 D보다 앞선 날짜만 쓴다. 조회는 목록 뒤(2026-10-02부터 08:38 목록 → 08:40 조회, 그 전 08:50 → 08:53)에 하므로 목록을 얼린 시각까지
들어온 행만 읽는 규칙(상한가 표시)은 적용할 수 없다 — 대신 D 개장 뒤에 받은 조회면 `fetched_after_open`으로 표시한다.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.calendar import Market, MarketCalendar
from app.core.clock import ensure_utc
from app.models.analyst import FETCH_TRUNCATED
from app.repositories import analyst_repo
from app.scoring.analyst import WINDOW_DAYS, Report, summarize

KR = MarketCalendar(Market.KR)
# 목표가 변화는 창 밖 이전 리포트와도 비교한다(조회 기간 183일 안).
HISTORY_DAYS = 183


def summaries(
    session: Session,
    day: date,
    instrument_ids: Collection[int],
    *,
    closes: Mapping[int, float] | None = None,
) -> dict[int, dict[str, Any]]:
    window_start = day - timedelta(days=WINDOW_DAYS)
    fetches = analyst_repo.usable_fetches(
        session, instrument_ids, day=day, window_start=window_start
    )
    if not fetches:
        return {}
    found = analyst_repo.reports(
        session, list(fetches), start=day - timedelta(days=HISTORY_DAYS), before=day
    )
    open_at = KR.session_open(KR.session_on_or_after(day))
    out: dict[int, dict[str, Any]] = {}
    for i, fetch in fetches.items():
        rows = [
            Report(
                id=r.id,
                report_date=r.report_date,
                broker=r.broker,
                opinion=r.opinion,
                target_price=float(r.target_price) if r.target_price is not None else None,
            )
            for r in found.get(i, [])
        ]
        summary = summarize(
            rows,
            day,
            prev_close=(closes or {}).get(i),
            truncated_before=fetch.oldest if fetch.status == FETCH_TRUNCATED else None,
        )
        summary["fetched_after_open"] = ensure_utc(fetch.fetched_at, field="fetched_at") > open_at
        out[i] = summary
    return out
