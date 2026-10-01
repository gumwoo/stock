""" "이런 일이 생기면 보통": 9시 시가 기준 첫 1시간의 지난 기록(판단 보조, 매매 권유 아님).

두 원천을 쓴다.
- **3개월 공시 기준표**(`app/reference/disclosure_reaction.json`): 2026-06-30~09-21 진입일의 사건 공시(공시 연구 표본)마다 09:00~10:00
  1분봉으로 잰 값. `reaction-reference` 명령이 한 번 만든다. 그 기간 한 장세·지금 상장된 종목만(생존편향)이라는 한계가 있다.
- **우리 목록 기록**: 아침 목록 날들의 1분봉(`minute_bar`)으로 같은 지표. 날마다 늘고, 처음 몇 주는 대부분 "기록 부족"이다.

지표는 `early_path.measure`(09:00 봉 시가 진입, 09:01부터 팔 수 있다고 봄) 하나로 잰다.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.calendar import Market, MarketCalendar
from app.models.watchlist import WatchlistMember, WatchlistSnapshot
from app.scoring import disclosure_events
from app.scoring.early_path import Bar, EarlyPath, measure
from app.scoring.watchlist import STRATEGY_VERSION_V2

KR = MarketCalendar(Market.KR)
SEOUL = ZoneInfo("Asia/Seoul")
REFERENCE = Path(__file__).resolve().parent.parent / "reference" / "disclosure_reaction.json"
MIN_N = 10
"""이보다 적으면 숫자 대신 "기록 부족"."""


@dataclass(frozen=True, slots=True)
class Stat:
    n: int
    hit_10: float
    """+2.5%에 10분 안에 닿은 비율(0~1)."""
    hit_60: float
    hit_minute: float | None
    """닿은 경우의 도달 시각(분) 중앙값."""
    low_before: float | None
    """닿은 경우, 닿기 전 최저 평균(%)."""
    miss_at_10: float | None
    """못 닿은 경우 10:00 평균(%)."""
    hit5_60: float
    gap_median: float | None = None
    """시가 갭 중앙값(전일 종가 대비 %). 기준표만."""

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}


def summarize(paths: Sequence[EarlyPath], gaps: Sequence[float] = ()) -> Stat:
    n = len(paths)
    hits = [p for p in paths if p.hit_25 is not None]
    miss = [p for p in paths if p.hit_25 is None]
    return Stat(
        n=n,
        hit_10=sum(p.hit_25_within(10) for p in paths) / n if n else 0.0,
        hit_60=len(hits) / n if n else 0.0,
        hit_minute=statistics.median(p.hit_25 for p in hits) if hits else None,  # type: ignore[type-var]
        low_before=statistics.fmean(p.low_before_25 for p in hits) if hits else None,
        miss_at_10=statistics.fmean(p.at_10 for p in miss) if miss else None,
        hit5_60=sum(p.hit_5 is not None for p in paths) / n if n else 0.0,
        gap_median=statistics.median(gaps) if gaps else None,
    )


def disclosure_key(report_nm: str) -> tuple[str, str] | None:
    """(세부 키, 묶음 키). 세부 = 맞은 문구(+자회사), 묶음 = 사건 종류·방향 — 세부가 기록 부족이면 묶음을 쓴다."""
    found = disclosure_events.matched(report_nm)
    if found is None:
        return None
    phrase, subsidiary, event = found
    s = event.sentiment or 0.0
    sign = "up" if s > 0 else "down" if s < 0 else "mixed"
    return (phrase + ("|자회사" if subsidiary else ""), f"{event.event_type}|{sign}")


# --- 기준표 만들기(한 번) ----------------------------------------------------------------------------------


def build_reference(
    session: Session, data: dict[str, Any], minutes: dict[str, Any], *, first: date, last: date
) -> dict[str, Any]:
    """공시 파일과 첫 1시간 1분봉 파일로 기준표를 만든다. 같은 종목·진입일·세부 키는 한 번만 센다.

    공시를 진입일에 놓는 규칙은 공시 연구(`disclosure_study_service.candidates`)와 같다: 접수일 다음 세션 시가.
    """
    from app.collectors.dart_fundamental import filed_date_from_receipt
    from app.repositories import instrument_repo
    from app.services import disclosure_study_service as study

    by_corp = {
        i.kr_corp_code: i.instrument_id
        for i in instrument_repo.list_active(session, asof=last, market=Market.KR, tracked=None)
        if i.kr_corp_code
    }
    events: list[tuple[int, date, str, str]] = []
    seen_rcept: set[str] = set()
    for row in data["rows"]:
        if row["rcept_no"] in seen_rcept:
            continue
        seen_rcept.add(row["rcept_no"])
        iid = by_corp.get(row["corp_code"])
        key = disclosure_key(row["report_nm"])
        filed_on = filed_date_from_receipt(row["rcept_no"])
        if iid is None or key is None or filed_on is None or not KR.covers(filed_on):
            continue
        entry = KR.next_session_open(filed_on).astimezone(SEOUL).date()
        if first <= entry <= last:
            events.append((iid, entry, key[0], key[1]))
    bars = study._daily_bars(session, sorted({e[0] for e in events}), first, last)
    seen: set[tuple[int, date, str]] = set()
    by_detail: dict[str, tuple[list[EarlyPath], list[float]]] = {}
    by_group: dict[str, tuple[list[EarlyPath], list[float]]] = {}
    group_seen: set[tuple[int, date, str]] = set()
    for iid, entry, detail, group in events:
        if (iid, entry, detail) in seen:
            continue
        seen.add((iid, entry, detail))
        got = minutes["days"].get(f"{iid}:{entry.isoformat()}")
        if not got or "error" in got:
            continue
        path = measure([Bar(_minute(b[0]), b[1], b[2], b[3], b[4]) for b in got["bars"]])
        if path is None:
            continue
        prev = KR.sessions_between(entry - timedelta(days=14), entry - timedelta(days=1))[-1]
        before = bars.get((iid, prev))
        gap = (path.entry / before[1] - 1) * 100 if before and before[1] > 0 else None
        targets = [(by_detail, detail)]
        if (iid, entry, group) not in group_seen:
            group_seen.add((iid, entry, group))
            targets.append((by_group, group))
        for table, k in targets:
            paths, gaps = table.setdefault(k, ([], []))
            paths.append(path)
            if gap is not None:
                gaps.append(gap)
    return {
        "meta": {
            "first_entry": first.isoformat(),
            "last_entry": last.isoformat(),
            "source": "공시 연구(2026-09-26) 파일: DART 주요사항·거래소공시, KIS 09:00~10:00 1분봉",
            "rule": "09:00 봉 시가 진입, 09:01부터 팔 수 있다고 봄. +2.5%에 처음 닿은 분, 닿기 전 최저(09:00 봉부터 그 봉까지), "
            "못 닿으면 10:00 전 마지막 종가. 같은 종목·진입일·종류는 한 번",
            "limits": "3개월 한 장세, 지금 상장된 종목만, 방향대로 장중에 움직이는 우위는 연구에서 확인 안 됨",
        },
        "detail": {k: summarize(*v).as_dict() for k, v in sorted(by_detail.items())},
        "group": {k: summarize(*v).as_dict() for k, v in sorted(by_group.items())},
    }


def _minute(hhmm: int) -> int:
    return (int(hhmm) // 100 - 9) * 60 + int(hhmm) % 100


# --- 읽기 ------------------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def reference() -> dict[str, Any]:
    try:
        loaded: dict[str, Any] = json.loads(REFERENCE.read_text(encoding="utf-8"))
        return loaded
    except (OSError, ValueError):
        return {"meta": {}, "detail": {}, "group": {}}


def disclosure_stat(report_nm: str) -> tuple[Stat | None, str]:
    """3개월 기준표의 값과 그 범위 이름("같은 공시" / "비슷한 공시 전체"). 둘 다 기록 부족이면 (None, "")."""
    key = disclosure_key(report_nm)
    if key is None:
        return None, ""
    ref = reference()
    for table, label in (
        (ref.get("detail", {}), "같은 공시"),
        (ref.get("group", {}), "비슷한 공시 전체"),
    ):
        got = table.get(key[0] if label == "같은 공시" else key[1])
        if got and got.get("n", 0) >= MIN_N:
            return Stat(**got), label
    return None, ""


def list_paths(session: Session, before: date) -> dict[tuple[date, int], EarlyPath]:
    """`before` 전 아침 목록 날들의 종목마다 첫 1시간 길(1분봉이 있는 것만). 10시 전 봉만 한 번에 읽는다."""
    pairs = set(
        session.execute(
            select(WatchlistSnapshot.session_date, WatchlistMember.instrument_id)
            .join(WatchlistMember, WatchlistMember.snapshot_id == WatchlistSnapshot.id)
            .where(
                WatchlistSnapshot.session_date < before,
                WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
            )
        )
        .tuples()
        .all()
    )
    if not pairs:
        return {}
    ordered = sorted(pairs)
    rows = session.execute(
        text(
            "select b.session_date, b.instrument_id, b.ts, b.open, b.high, b.low, b.close "
            "from unnest(cast(:days as date[]), cast(:ids as bigint[])) as p(d, i) "
            "join minute_bar b on b.session_date = p.d and b.instrument_id = p.i "
            "where (b.ts at time zone 'Asia/Seoul')::time < time '10:00' "
            "order by b.session_date, b.instrument_id, b.ts"
        ),
        {"days": [d for d, _ in ordered], "ids": [i for _, i in ordered]},
    ).all()
    bars: dict[tuple[date, int], list[Bar]] = {}
    for day, iid, *ohlc in rows:
        bars.setdefault((day, iid), []).append(to_bar(*ohlc))
    out: dict[tuple[date, int], EarlyPath] = {}
    for key, got in bars.items():
        path = measure(got)
        if path is not None:
            out[key] = path
    return out


def to_bar(ts: Any, o: Any, h: Any, lo: Any, c: Any) -> Bar:
    """minute_bar 한 행을 09:00부터 지난 분의 봉으로."""
    local = ts.astimezone(SEOUL)
    return Bar((local.hour - 9) * 60 + local.minute, float(o), float(h), float(lo), float(c))


def fmt(stat: Stat | None, scope: str, *, short: bool = False) -> str:
    """기록 문장. 2인칭·지시 없이 기록체로."""
    if stat is None:
        return "기록 부족"
    hit = f"{stat.hit_60 * 100:.0f}%"
    soon = f"10분 내 {stat.hit_10 * 100:.0f}%"
    when = f"({soon}, 보통 {stat.hit_minute:.0f}분)" if stat.hit_minute is not None else f"({soon})"
    low = f"닿기 전 평균 {stat.low_before:+.1f}%" if stat.low_before is not None else ""
    miss = f"못 닿으면 10시 평균 {stat.miss_at_10:+.1f}%" if stat.miss_at_10 is not None else ""
    if short:  # 카톡용: "9시 시가 기준"은 안내 메시지가 말한다
        soon = f"10분 {stat.hit_10 * 100:.0f}%"
        when = (
            f"({soon}·보통 {stat.hit_minute:.0f}분)" if stat.hit_minute is not None else f"({soon})"
        )
        parts = [
            f"{scope} {stat.n}건: 1시간 내 +2.5% {hit}{when}",
            f"닿기 전 {stat.low_before:+.1f}%" if stat.low_before is not None else "",
            f"못 닿으면 10시 {stat.miss_at_10:+.1f}%" if stat.miss_at_10 is not None else "",
        ]
    else:
        gap = f"시가는 보통 {stat.gap_median:+.1f}%로 시작. " if stat.gap_median is not None else ""
        parts = [
            f"{scope} {stat.n}건 기록: {gap}9시 시가 기준 1시간 안에 +2.5%까지 간 경우 {hit}{when}",
            low,
            miss,
        ]
    return "·".join(p for p in parts if p)


def group_paths(
    paths: Iterable[tuple[str, EarlyPath]],
) -> dict[str, Stat]:
    grouped: dict[str, list[EarlyPath]] = {}
    for key, p in paths:
        grouped.setdefault(key, []).append(p)
    return {k: summarize(v) for k, v in grouped.items()}
