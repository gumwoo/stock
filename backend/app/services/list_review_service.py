"""목록 하루의 사후 기술 통계(읽기만). 목록 가설 H1~H7의 판정이 아니다.

지수 대형주(그 시장 시가총액 5% 이상)는 모든 묶음에서 빼고 따로 본다: 지수 대비로 재면 자기 자신과 비교하는 셈이고,
개별 뉴스보다 업황·시장 흐름의 영향이 크다. 대형주를 뗀 묶음은 사전 등록 표본(H5·H7은 대형주 포함)과 다르다.

값: 첫 1시간(첫 봉 시가 → 10시 전 마지막 종가), 시가→종가, 지수 대비(시가→종가 - 같은 시장 지수), 장중 최고(사후값,
그 가격에 팔 수 있었다는 뜻이 아니다). 원천은 장 마감 뒤 REST 1분봉 요약(`intraday_summary`, 지금 분석 버전·COMPLETE만).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instrument
from app.models.intraday import IntradaySummary
from app.models.watchlist import WatchlistMember, WatchlistSnapshot
from app.repositories import minute_repo
from app.scoring.intraday import ANALYSIS_VERSION
from app.scoring.watchlist import SELECTION_VERSION_V2, STRATEGY_VERSION_V2
from app.services import heavyweight_service, overnight_service


@dataclass(frozen=True, slots=True)
class Row:
    name: str
    action: str
    reasons: tuple[str, ...]
    heavyweight: bool
    weight_pct: float | None
    first_hour: float
    open_close: float
    market: float | None
    mfe: float | None


@dataclass(frozen=True, slots=True)
class Stats:
    label: str
    n: int
    first_hour: float | None
    open_close: float | None
    vs_market: float | None
    up_close: int
    mfe_median: float | None


def _mean(xs: Sequence[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def stats(label: str, rows: Sequence[Row]) -> Stats:
    """한 묶음의 요약. 순수."""
    vs = [r.open_close - r.market for r in rows if r.market is not None]
    mfe = [r.mfe for r in rows if r.mfe is not None]
    return Stats(
        label=label,
        n=len(rows),
        first_hour=_mean([r.first_hour for r in rows]),
        open_close=_mean([r.open_close for r in rows]),
        vs_market=_mean(vs),
        up_close=sum(1 for r in rows if r.open_close > 0),
        mfe_median=statistics.median(mfe) if mfe else None,
    )


def groups(rows: Sequence[Row]) -> list[Stats]:
    """대형주를 뺀 묶음(전체·판단별·이유별)과 대형주 묶음. 순수."""
    rest = [r for r in rows if not r.heavyweight]
    out = [stats("목록(대형주 뺌)", rest)]
    for action in ("BUY_INTEREST", "WATCH", "CAUTION", "ABSTAINED", "NONE"):
        g = [r for r in rest if r.action == action]
        if g:
            out.append(stats(f"판단 {action}", g))
    for reason in sorted({x for r in rest for x in r.reasons}):
        out.append(stats(f"이유 {reason}", [r for r in rest if reason in r.reasons]))
    heavy = [r for r in rows if r.heavyweight]
    if heavy:
        out.append(stats("지수 대형주", heavy))
    return out


@dataclass
class Review:
    day: date
    rows: list[Row] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    after_the_fact: bool = False
    rank_day: date | None = None
    semis: dict[str, Any] | None = None


def review(session: Session, day: date) -> Review | None:
    snap = session.execute(
        select(WatchlistSnapshot).where(
            WatchlistSnapshot.session_date == day,
            WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
            WatchlistSnapshot.selection_version == SELECTION_VERSION_V2,
        )
    ).scalar_one_or_none()
    if snap is None:
        return None
    members = session.execute(
        select(WatchlistMember, Instrument.name)
        .join(Instrument, Instrument.instrument_id == WatchlistMember.instrument_id)
        .where(WatchlistMember.snapshot_id == snap.id)
        .order_by(WatchlistMember.rank)
    ).all()
    ids = [m.instrument_id for m, _ in members]
    summaries = {
        x.instrument_id: x
        for x in session.execute(
            select(IntradaySummary).where(
                IntradaySummary.session_date == day,
                IntradaySummary.analysis_version == ANALYSIS_VERSION,
                IntradaySummary.status == minute_repo.COMPLETE,
                IntradaySummary.instrument_id.in_(ids),
            )
        ).scalars()
    }
    # 사후 분석이므로 이전 순위표가 없으면 그날(또는 뒤) 표로 뗀다 — 출력에 "사후 판정"으로 적는다.
    weights = heavyweight_service.weights_for(session, day, ids, allow_after=True)
    out = Review(day=day)
    for m, name in members:
        s = summaries.get(m.instrument_id)
        if s is None or s.first_hour_pct is None or s.return_pct is None:
            out.missing.append(name)
            continue
        w = weights.get(m.instrument_id)
        out.rows.append(
            Row(
                name=name,
                action=m.last_action or "NONE",
                reasons=tuple(m.reasons),
                heavyweight=bool(w and w.heavyweight),
                weight_pct=w.weight_pct if w else None,
                first_hour=s.first_hour_pct,
                open_close=s.return_pct,
                market=s.market_return_pct,
                mfe=s.mfe_pct,
            )
        )
    any_w = next(iter(weights.values()), None)
    if any_w is not None:
        out.after_the_fact = any_w.after_the_fact
        out.rank_day = any_w.rank_day
    out.semis = overnight_service.us_semis(session, day)
    return out
