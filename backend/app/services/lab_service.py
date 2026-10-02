"""전략 실험실 화면의 숫자: 아침 목록 기록(매일 늘어남)과 3개월 공시 기준표. 읽기만 한다.

- 우리 목록: PREOPEN_V2 목록 날마다 목록 종목의 09:00~09:59 1분봉(`minute_bar`, 장 마감 뒤 16:20 작업이 받는다), 전 거래일
  종가·등락(`price_limit_service.prev_limits_many`, 목록을 얼린 시각까지 들어온 수정본), 이유·점수·순위.
- 3개월 기준표: `app/reference/first_hour_rules.json`(`rule-reference` 명령이 공시 연구 파일로 한 번 만든다).

계산에서 빼는 종목(날짜별로 센다): 1분봉 수집 기록이 아직 없음(그날 작업이 그 종목까지 안 감 → 그날은 "수집 중"),
부분 수집(PARTIAL·ERROR가 최신), 09:00 봉 없음(첫 체결이 늦음, 개장 VI 등), 시초 잠김(09:30 전 모든 봉이 한 가격 — 시가에 살 수
없었다). 지수 대형주도 넣는다(`list-review`는 묶음에서 뺀다).
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.calendar import Market, MarketCalendar
from app.models import Instrument
from app.models.intraday import IntradaySummary, MinuteFetch
from app.models.watchlist import WatchlistMember, WatchlistSnapshot
from app.scoring import disclosure_events, lab
from app.scoring.entry_rules import Bar
from app.scoring.watchlist import SELECTION_VERSION_V2, STRATEGY_VERSION_V2
from app.services import intraday_service, price_limit_service

logger = logging.getLogger(__name__)
KR = MarketCalendar(Market.KR)
SEOUL = ZoneInfo("Asia/Seoul")
REFERENCE = Path(__file__).resolve().parent.parent / "reference" / "first_hour_rules.json"
SETTLED = ("COMPLETE", "EMPTY")


def hhmm(ts: datetime) -> str:
    local = ts.astimezone(SEOUL)
    return f"{local.hour:02d}{local.minute:02d}"


# --- 우리 목록 표본 -------------------------------------------------------------------------------------------


def _snapshots(session: Session) -> list[WatchlistSnapshot]:
    return list(
        session.execute(
            select(WatchlistSnapshot)
            .where(
                WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
                WatchlistSnapshot.selection_version == SELECTION_VERSION_V2,
            )
            .order_by(WatchlistSnapshot.session_date)
        ).scalars()
    )


def _first_hours(
    session: Session, pairs: Sequence[tuple[date, int]]
) -> dict[tuple[date, int], list[Bar]]:
    if not pairs:
        return {}
    rows = session.execute(
        text(
            "select b.session_date, b.instrument_id, b.ts, b.open, b.high, b.low, b.close "
            "from unnest(cast(:days as date[]), cast(:ids as bigint[])) as p(d, i) "
            "join minute_bar b on b.session_date = p.d and b.instrument_id = p.i "
            "where (b.ts at time zone 'Asia/Seoul')::time < time '10:00' "
            "order by b.session_date, b.instrument_id, b.ts"
        ),
        {"days": [d for d, _ in pairs], "ids": [i for _, i in pairs]},
    ).all()
    out: dict[tuple[date, int], list[Bar]] = defaultdict(list)
    for d, i, ts, o, h, lo, c in rows:
        out[(d, i)].append((hhmm(ts), float(o), float(h), float(lo), float(c)))
    return out


def _fetch_status(
    session: Session, pairs: Sequence[tuple[date, int]]
) -> dict[tuple[date, int], str]:
    if not pairs:
        return {}
    days = sorted({d for d, _ in pairs})
    ids = sorted({i for _, i in pairs})
    f = MinuteFetch
    rows = session.execute(
        select(f.session_date, f.instrument_id, f.status)
        .where(f.session_date.in_(days), f.instrument_id.in_(ids))
        .distinct(f.instrument_id, f.session_date)
        .order_by(f.instrument_id, f.session_date, f.fetched_at.desc(), f.id.desc())
    ).all()
    return {(d, i): s for d, i, s in rows}


def our_samples(session: Session) -> tuple[list[lab.Sample], list[dict[str, Any]]]:
    """목록 날마다의 표본과 날짜별 상태(측정 수, 제외 이유, 수집 중인가)."""
    snaps = _snapshots(session)
    members: dict[int, list[WatchlistMember]] = defaultdict(list)
    if snaps:
        for m in session.execute(
            select(WatchlistMember).where(WatchlistMember.snapshot_id.in_([s.id for s in snaps]))
        ).scalars():
            members[m.snapshot_id].append(m)
    pairs = [(s.session_date, m.instrument_id) for s in snaps for m in members[s.id]]
    bars = _first_hours(session, pairs)
    status = _fetch_status(session, pairs)
    ids = sorted({i for _, i in pairs})
    listing = {
        i: (v.value if v is not None else None)
        for i, v in session.execute(
            select(Instrument.instrument_id, Instrument.listing).where(
                Instrument.instrument_id.in_(ids)
            )
        ).all()
    }
    samples: list[lab.Sample] = []
    days: list[dict[str, Any]] = []
    for snap in snaps:
        d = snap.session_date
        group = members[snap.id]
        prev = price_limit_service.prev_limits_many(
            session, d, [m.instrument_id for m in group], ingested_before=snap.asof
        )
        excluded = lab.Exclusions()
        pending = False
        day_samples: list[lab.Sample] = []
        for m in group:
            got = bars.get((d, m.instrument_id), [])
            st = status.get((d, m.instrument_id))
            if st is None and not got:
                pending = True
                excluded.add("수집 전")
                continue
            if st is not None and st not in SETTLED:
                # 최신 수집이 PARTIAL·ERROR면 남은 봉이 있어도 재지 않는다
                excluded.add("부분 수집")
                continue
            if not got:
                excluded.add("1분봉 없음")
                continue
            if got[0][0] != "0900":
                excluded.add("09:00 봉 없음")
                continue
            if lab.locked(got):
                excluded.add("시초 잠김(살 수 없었음)")
                continue
            p = prev.get(m.instrument_id)
            day_samples.append(
                lab.Sample(
                    day=d,
                    bars=tuple(got),
                    market=listing.get(m.instrument_id),
                    gap=(got[0][1] / p.close - 1) * 100 if p and p.close > 0 else None,
                    prev_change=p.change_pct if p else None,
                    reasons=tuple(m.reasons or ()),
                    technical=m.technical_score,
                    fundamental=m.fundamental_score,
                    total=m.total_score,
                    rank=m.rank,
                )
            )
        samples += day_samples
        days.append(_day_row(d, snap.asof, len(group), day_samples, excluded, pending))
    return samples, days


def _day_row(
    d: date,
    asof: datetime,
    members: int,
    samples: Sequence[lab.Sample],
    excluded: lab.Exclusions,
    pending: bool,
) -> dict[str, Any]:
    def mean(values: Sequence[float | None]) -> float | None:
        got = [v for v in values if v is not None]
        return statistics.fmean(got) if got else None

    return {
        "day": d.isoformat(),
        "asof": asof.isoformat(),
        "members": members,
        "measured": len(samples),
        "kospi": sum(1 for s in samples if s.market == "KOSPI"),
        "kosdaq": sum(1 for s in samples if s.market == "KOSDAQ"),
        "excluded": excluded.counts,
        "pending": pending,
        "hit25_10": lab.hit_rate(samples, 0.025, within="0910"),
        "hit25_60": lab.hit_rate(samples, 0.025),
        "hit5_60": lab.hit_rate(samples, 0.05),
        "rules": [
            {"take": t, "stop": st, "mean": mean([lab.ret(s.bars, t, st) for s in samples])}
            for t, st in lab.HEADLINE
        ],
        "at_ten": mean([lab.at_ten(s.bars) for s in samples]),
    }


# --- 3개월 기준표 --------------------------------------------------------------------------------------------


def build_reference(
    session: Session, data: dict[str, Any], minutes: dict[str, Any], *, first: date, last: date
) -> dict[str, Any]:
    """공시 연구 파일로 기준표를 만든다. (종목, 진입일)은 한 번만 센다. 공시 방향은 그날 공시 중 가장 강한 것
    (`disclosure_study.strongest`와 같은 순서: 강도, |방향|, 사건 종류)."""
    from app.collectors.dart_fundamental import filed_date_from_receipt
    from app.repositories import instrument_repo
    from app.services import disclosure_study_service as study

    actives = instrument_repo.list_active(session, asof=last, market=Market.KR, tracked=None)
    by_corp = {i.kr_corp_code: i.instrument_id for i in actives if i.kr_corp_code}
    listing = {i.instrument_id: (i.listing.value if i.listing else None) for i in actives}
    events: dict[tuple[int, date], list[disclosure_events.DisclosureEvent]] = defaultdict(list)
    seen: set[str] = set()
    for row in data["rows"]:
        if row["rcept_no"] in seen:
            continue
        seen.add(row["rcept_no"])
        iid = by_corp.get(row["corp_code"])
        ev = disclosure_events.classify(row["report_nm"])
        filed_on = filed_date_from_receipt(row["rcept_no"])
        if iid is None or ev is None or filed_on is None or not KR.covers(filed_on):
            continue
        entry = KR.next_session_open(filed_on).astimezone(SEOUL).date()
        if first <= entry <= last:
            events[(iid, entry)].append(ev)
    ids = sorted({i for i, _ in events})
    start = KR.sessions_between(first - timedelta(days=50), first)[0]
    daily = study._daily_bars(session, ids, start, last)
    samples: list[lab.Sample] = []
    for (iid, d), evs in sorted(events.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        got = minutes["days"].get(f"{iid}:{d.isoformat()}")
        if not got or "error" in got:
            continue
        bars: list[Bar] = sorted(
            (
                (str(b[0]).zfill(4), float(b[1]), float(b[2]), float(b[3]), float(b[4]))
                for b in got["bars"]
                if str(b[0]).zfill(4) < "1000"
            ),
            key=lambda x: x[0],
        )
        if not bars or bars[0][0] != "0900" or lab.locked(bars):
            continue
        sess = KR.sessions_between(d - timedelta(days=45), d - timedelta(days=1))
        p1 = daily.get((iid, sess[-1])) if sess else None
        p2 = daily.get((iid, sess[-2])) if len(sess) > 1 else None
        vols = [daily[(iid, x)][2] for x in sess[-21:-1] if (iid, x) in daily]
        avg_vol = statistics.fmean(vols) if vols else 0.0
        top = max(evs, key=lambda e: (e.intensity, abs(e.sentiment or 0.0), e.event_type))
        s = top.sentiment or 0.0
        samples.append(
            lab.Sample(
                day=d,
                bars=tuple(bars),
                market=listing.get(iid),
                gap=(bars[0][1] / p1[1] - 1) * 100 if p1 and p1[1] > 0 else None,
                prev_change=(p1[1] / p2[1] - 1) * 100 if p1 and p2 and p2[1] > 0 else None,
                volume_ratio=p1[2] / avg_vol if p1 and avg_vol > 0 else None,
                price=bars[0][1],
                direction="좋음" if s > 0 else "나쁨" if s < 0 else "애매",
            )
        )
    return {
        "meta": {
            "first_entry": first.isoformat(),
            "last_entry": last.isoformat(),
            "samples": len(samples),
            "days": len({s.day for s in samples}),
            "source": "공시 연구(2026-09-26) 파일: DART 주요사항·거래소공시, KIS 09:00~10:00 1분봉, 일봉(YFINANCE)",
            "rule": "9시 시가 진입, entry_rules.r3 체결(09:01부터), 비용 0.30%, 날짜별 평균이 관측 하나. (종목, 진입일) 한 번",
            "limits": "3개월 한 장세, 공시가 있던 종목만, 지금 상장된 종목만. 탐색이지 판정이 아니다",
        },
        "grid": lab.grid(samples),
        "conditions": lab.conditions(samples, lab.REFERENCE_FEATURES),
        "hypotheses": {
            h.key: lab.rule_stat([x for x in samples if h.select(x)], h.take, h.stop).as_dict()
            for h in lab.HYPOTHESES
        },
    }


@lru_cache(maxsize=1)
def reference() -> dict[str, Any]:
    try:
        loaded: dict[str, Any] = json.loads(REFERENCE.read_text(encoding="utf-8"))
        return loaded
    except (OSError, ValueError):
        return {"meta": {}, "grid": [], "conditions": [], "hypotheses": {}}


# --- 화면 ----------------------------------------------------------------------------------------------------

_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def _key(session: Session) -> tuple[Any, ...]:
    return (
        session.execute(select(func.max(MinuteFetch.id))).scalar(),
        session.execute(select(func.max(WatchlistSnapshot.id))).scalar(),
        session.execute(select(func.max(IntradaySummary.id))).scalar(),
    )


def lab_view(session: Session) -> dict[str, Any]:
    """화면 한 번에 필요한 것 전부. 1분봉·목록·요약이 새로 들어오기 전까지는 계산해 둔 것을 준다."""
    key = _key(session)
    cached = _CACHE.get(key)  # 한 번만 읽는다(다른 요청이 사이에 비워도 KeyError가 나지 않게)
    if cached is not None:
        return cached
    samples, days = our_samples(session)
    split = _frozen(samples, days)
    after = [s for s, frozen in split if frozen]
    before = [s for s, frozen in split if not frozen]
    ref = reference()
    out = {
        "cost_pct": lab.COST * 100,
        "frozen_at": lab.FROZEN_AT.isoformat(),
        "days": days,
        "grid": {"ours": lab.grid(samples), "reference": ref.get("grid", [])},
        "conditions": {
            "ours": lab.conditions(samples, lab.OUR_FEATURES),
            "reference": ref.get("conditions", []),
        },
        "hypotheses": [
            {
                "key": h.key,
                "text": h.text,
                "take": h.take,
                "stop": h.stop,
                "sign": h.sign,
                "basis": h.basis,
                "reference": ref.get("hypotheses", {}).get(h.key),
                "before": lab.rule_stat(
                    [s for s in before if h.select(s)], h.take, h.stop
                ).as_dict(),
                "after": lab.judge(h, after).as_dict(),
            }
            for h in lab.HYPOTHESES
        ],
        "list_hypotheses": _list_hypotheses(session),
        "reference_meta": ref.get("meta", {}),
    }
    _CACHE.clear()
    _CACHE[key] = out
    return out


def _frozen(
    samples: Sequence[lab.Sample], days: Sequence[dict[str, Any]]
) -> list[tuple[lab.Sample, bool]]:
    """목록을 얼린 시각이 가설 고정 뒤인 날의 표본인가."""
    after = {r["day"] for r in days if datetime.fromisoformat(str(r["asof"])) > lab.FROZEN_AT}
    return [(s, s.day.isoformat() in after) for s in samples]


def _list_hypotheses(session: Session) -> list[dict[str, Any]]:
    """아침 목록 가설 H1~H7(2026-09-26 고정, 첫 봉 시가·비용 전). `intraday_service.report` 그대로."""
    try:
        with session.begin_nested():
            results = intraday_service.report(session).results
    except Exception:  # 보조 섹션: 실패해도 나머지 화면은 나간다
        logger.exception("lab: list hypotheses failed")
        return []
    return [
        {
            "key": r.key,
            "text": r.text,
            "days": r.days,
            "mean": r.mean,
            "t": r.t,
            "first": r.halves[0],
            "second": r.halves[1],
            "state": r.state,
        }
        for r in results
    ]
