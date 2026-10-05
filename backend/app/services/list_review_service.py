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
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instrument
from app.models.intraday import IntradaySummary, MinuteBar
from app.models.watchlist import WatchlistMember, WatchlistSnapshot
from app.repositories import minute_repo
from app.scoring import disclosure_events, event_explain, price_limit
from app.scoring.early_path import EarlyPath, measure
from app.scoring.intraday import ANALYSIS_VERSION
from app.scoring.watchlist import (
    SELECTION_VERSION_V2_EXCLUDE,
    SELECTION_VERSIONS_V2,
    STRATEGY_VERSION_V2,
)
from app.services import (
    analyst_service,
    briefing_service,
    event_brief_service,
    heavyweight_service,
    overnight_service,
    price_limit_service,
    reaction_service,
)

SEOUL = ZoneInfo("Asia/Seoul")


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
    prev_limit: str | None = None
    """전 거래일 상한가 상태(LOCKED / CLOSED / TOUCHED)."""
    gap: float | None = None
    """그날 첫 1분봉 시가 / 전 거래일 종가 - 1(%)."""
    reports: int | None = None
    """목록 날 전 90일 증권사 리포트 수. None은 조회하지 못함(0과 다르다)."""
    peak_930: float | None = None
    """첫 1분봉 시가 대비 09:30 전 1분봉 고가의 최댓값(%). 사후 최고값이지 팔 수 있었던 가격이 아니다."""
    tops: tuple[str, ...] = ()
    """그날 점수 순위(현재 브리핑 규칙을 08:40 저장 점수에 사후 적용): "기술1", "종합2" 등."""
    locked_open: bool = False
    """09:30 전 모든 1분봉이 한 가격(시초 상한가 잠김 등) — 사실상 살 수 없었다."""
    early: EarlyPath | None = None
    """09:00 봉 시가 진입 기준 첫 1시간(+2.5%/+5% 도달, 닿기 전 최저, 10시). 09:00 봉이 없으면 None."""
    kinds: tuple[str, ...] = ()
    """목록 이유가 된 공시의 쉬운 종류(예: "유상증자", "자회사 유상증자")."""
    excluded: str | None = None
    """선정 3에서 목록에서 뺀 이유(LOW_SCORE / PREV_SURGE, 둘이면 쉼표). 남은 종목은 None."""
    screened: bool = False
    """선정 3(제외 규칙을 적용한) 목록 날인가."""


@dataclass(frozen=True, slots=True)
class Stats:
    label: str
    n: int
    first_hour: float | None
    open_close: float | None
    vs_market: float | None
    up_close: int
    mfe_median: float | None
    peak_930: float | None = None
    peak_930_2pct: int = 0
    hit25_10: float | None = None
    """+2.5%에 10분 안에 닿은 비율(09:00 봉이 있는 종목 중)."""
    hit25_60: float | None = None
    miss_at10: float | None = None
    """+2.5%에 1시간 안에 못 닿은 종목의 10:00 평균(%)."""


def _mean(xs: Sequence[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _rate(flags: Sequence[bool]) -> float | None:
    return sum(flags) / len(flags) if flags else None


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
        peak_930=_mean([r.peak_930 for r in rows if r.peak_930 is not None]),
        peak_930_2pct=sum(1 for r in rows if r.peak_930 is not None and r.peak_930 >= 2.0),
        hit25_10=_rate([r.early.hit_25_within(10) for r in rows if r.early]),
        hit25_60=_rate([r.early.hit_25 is not None for r in rows if r.early]),
        miss_at10=_mean([r.early.at_10 for r in rows if r.early and r.early.hit_25 is None]),
    )


LIMIT_LABELS = {
    price_limit.LOCKED: "전일 점상한가",
    price_limit.CLOSED: "전일 상한가 마감(장중 거래)",
    price_limit.TOUCHED: "전일 상한가 터치",
}


def groups(rows: Sequence[Row]) -> list[Stats]:
    """대형주를 뺀 묶음(전체·판단별·이유별)과 대형주 묶음. 순수."""
    rest = [r for r in rows if not r.heavyweight]
    out = [stats("목록(대형주 뺌)", rest)]
    # 선정 3(2026-10-06~): 목록에 남은 종목과 뺀 종목을 따로(뺀 규칙이 계속 맞는지 보려고).
    screened = [r for r in rest if r.screened]  # 선정 2 날은 "남음/뺌"으로 나눌 수 없다
    if screened:
        out.append(stats("목록에 남음", [r for r in screened if not r.excluded]))
        for code, label in (
            ("LOW_SCORE", "뺌: 판단 점수 40 미만"),
            ("PREV_SURGE", "뺌: 전일 +15% 이상"),
            ("GAP_UP", "뺌: 예상 갭 +3% 이상(08:50)"),
        ):
            g = [r for r in screened if r.excluded and code in r.excluded]
            if g:
                out.append(stats(label, g))
    for action in ("BUY_INTEREST", "WATCH", "CAUTION", "ABSTAINED", "NONE"):
        g = [r for r in rest if r.action == action]
        if g:
            out.append(stats(f"판단 {action}", g))
    for reason in sorted({x for r in rest for x in r.reasons}):
        out.append(stats(f"이유 {reason}", [r for r in rest if reason in r.reasons]))
    with_reports = [r for r in rest if r.reports]
    without = [r for r in rest if r.reports == 0]
    if with_reports:
        out.append(stats("증권사 리포트 있음(90일)", with_reports))
    if without:
        out.append(stats("증권사 리포트 없음(조회됨)", without))
    for code, label in LIMIT_LABELS.items():
        g = [r for r in rest if r.prev_limit == code]
        if g:
            out.append(stats(label, g))
    # 시가 갭 구간, 지수가 오른 날·내린 날(장 분위기에 따라 갈리는지 보려고), 공시 쉬운 종류.
    for lo, hi, label in (
        (-99.0, -2.0, "갭 -2% 미만"),
        (-2.0, 0.0, "갭 -2~0%"),
        (0.0, 2.0, "갭 0~+2%"),
        (2.0, 99.0, "갭 +2% 이상"),
    ):
        g = [r for r in rest if r.gap is not None and lo <= r.gap < hi]
        if g:
            out.append(stats(label, g))
    for up, label in ((True, "지수 오른 날"), (False, "지수 내린·보합 날")):
        g = [r for r in rest if r.market is not None and (r.market > 0) == up]
        if g:
            out.append(stats(label, g))
    for kind in sorted({k for r in rest for k in r.kinds}):
        out.append(stats(f"공시 {kind}", [r for r in rest if kind in r.kinds]))
    heavy = [r for r in rows if r.heavyweight]
    if heavy:
        out.append(stats("지수 대형주", heavy))
    # 점수 TOP3: 대형주를 빼지 않는다(순위 그대로를 잰다).
    for cat in ("기술", "재무", "종합"):
        g = [r for r in rows if any(t.startswith(cat) for t in r.tops)]
        if g:
            out.append(stats(f"{cat} TOP3(현재 규칙, 대형주 포함)", g))
    return out


@dataclass
class Review:
    day: date
    rows: list[Row] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    after_the_fact: bool = False
    rank_day: date | None = None
    semis: dict[str, Any] | None = None
    tops: dict[str, list[tuple[str, str, Row | None]]] = field(default_factory=dict)
    """점수 순위별 (표시, 이름, 행). 1분봉 요약이 없어 빠진 종목은 행이 None."""


def list_days(session: Session, start: date, end: date) -> list[date]:
    """기간 안 V2 아침 목록 날짜(달력이 아니라 목록이 있는 날)."""
    return list(
        session.execute(
            select(WatchlistSnapshot.session_date)
            .where(
                WatchlistSnapshot.session_date >= start,
                WatchlistSnapshot.session_date <= end,
                WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
                WatchlistSnapshot.selection_version.in_(SELECTION_VERSIONS_V2),
            )
            .order_by(WatchlistSnapshot.session_date)
        ).scalars()
    )


def peak_930(
    bars: Sequence[tuple[datetime, Decimal, Decimal, Decimal]],
) -> tuple[float | None, bool]:
    """(첫 봉 시가 대비 09:30 전 고가 최댓값 %, 09:30 전이 한 가격에 잠겼는가). bars: (ts, open, high, low), 시각 순. 순수."""
    if not bars:
        return None, False
    first_open = float(bars[0][1])
    early = [b for b in bars if b[0].astimezone(SEOUL).time() < time(9, 30)]
    if not early or first_open <= 0:
        return None, False
    high = max(float(b[2]) for b in early)
    low = min(float(b[3]) for b in early)
    return (high / first_open - 1) * 100, high == low


def _ranks(members: Sequence[Any]) -> dict[int, list[str]]:
    """목록 행(08:40 저장값)에 현재 브리핑 순위 규칙을 적용한 표시."""
    rows = [
        {
            "instrument_id": m.instrument_id,
            "rank": m.rank,
            "detail": m.score_detail,
            "action": m.last_action,
            "total_score": m.total_score,
            "weight_total": briefing_service.weight_total(m.score_detail),
            "technical_score": m.technical_score,
            "fundamental_score": m.fundamental_score,
        }
        for m, _ in members
    ]
    out: dict[int, list[str]] = {}
    for cat, top in briefing_service.rankings(rows).items():
        for n, r in enumerate(top, 1):
            out.setdefault(int(r["instrument_id"]), []).append(f"{cat}{n}")
    return out


def review(session: Session, day: date, *, with_semis: bool = True) -> Review | None:
    snap = session.execute(
        select(WatchlistSnapshot).where(
            WatchlistSnapshot.session_date == day,
            WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
            WatchlistSnapshot.selection_version.in_(SELECTION_VERSIONS_V2),
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
    limits = price_limit_service.prev_limits(session, day, ids)
    analysts = analyst_service.summaries(session, day, ids)
    first_bars = session.execute(
        select(MinuteBar.instrument_id, MinuteBar.open)
        .where(MinuteBar.session_date == day, MinuteBar.instrument_id.in_(ids))
        .order_by(MinuteBar.instrument_id, MinuteBar.ts)
        .distinct(MinuteBar.instrument_id)
    ).all()
    opens: dict[int, Decimal] = {row[0]: row[1] for row in first_bars}
    bars_by: dict[int, list[tuple[datetime, Decimal, Decimal, Decimal]]] = {}
    closes: dict[int, list[Any]] = {}
    for i, ts, o, h, lo, c in session.execute(
        select(
            MinuteBar.instrument_id,
            MinuteBar.ts,
            MinuteBar.open,
            MinuteBar.high,
            MinuteBar.low,
            MinuteBar.close,
        )
        .where(MinuteBar.session_date == day, MinuteBar.instrument_id.in_(ids))
        .order_by(MinuteBar.instrument_id, MinuteBar.ts)
    ).all():
        bars_by.setdefault(i, []).append((ts, o, h, lo))
        closes.setdefault(i, []).append((ts, o, h, lo, c))
    kinds: dict[int, set[str]] = {}
    for i, evs in event_brief_service.disclosure_events_for(session, day, snap.asof, ids).items():
        for e in evs:
            found = disclosure_events.matched(e["report_nm"])
            if found is not None:
                kinds.setdefault(i, set()).add(event_explain.disclosure(found[0], found[1]).kind)
    # 점수 TOP3는 카톡과 같게 목록에 남은 종목으로만 매긴다(행과 묶음은 제외 전 40개 그대로).
    # 갭 판정(08:50, GAP_UP)으로 뺀 종목은 08:44 카톡 순위에는 있었다 — 점수·전일 급등으로 뺀 것만 뺀다.
    ranks = _ranks(
        [(m, name) for m, name in members if not m.excluded_reason or m.excluded_reason == "GAP_UP"]
    )
    out = Review(day=day)
    by_id: dict[int, Row] = {}
    for m, name in members:
        s = summaries.get(m.instrument_id)
        if s is None or s.first_hour_pct is None or s.return_pct is None:
            out.missing.append(name)
            continue
        w = weights.get(m.instrument_id)
        lim = limits.get(m.instrument_id)
        first = opens.get(m.instrument_id)
        gap = (float(first) / lim.close - 1) * 100 if lim and first else None
        peak, locked = peak_930(bars_by.get(m.instrument_id, []))
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
                prev_limit=lim.state if lim else None,
                gap=gap,
                reports=analysts[m.instrument_id]["count"] if m.instrument_id in analysts else None,
                peak_930=peak,
                tops=tuple(ranks.get(m.instrument_id, ())),
                locked_open=locked,
                early=measure(
                    [reaction_service.to_bar(*b) for b in closes.get(m.instrument_id, [])]
                ),
                kinds=tuple(sorted(kinds.get(m.instrument_id, ()))),
                excluded=m.excluded_reason,
                screened=snap.selection_version == SELECTION_VERSION_V2_EXCLUDE,
            )
        )
        by_id[m.instrument_id] = out.rows[-1]
    any_w = next(iter(weights.values()), None)
    if any_w is not None:
        out.after_the_fact = any_w.after_the_fact
        out.rank_day = any_w.rank_day
    names = {m.instrument_id: name for m, name in members}
    for i, tags in ranks.items():
        for t in tags:
            out.tops.setdefault(t[:2], []).append((t, names[i], by_id.get(i)))
    for entries in out.tops.values():
        entries.sort(key=lambda e: e[0])
    if with_semis:
        out.semis = overnight_service.us_semis(session, day)
    return out
