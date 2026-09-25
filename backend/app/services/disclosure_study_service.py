"""공시 이벤트 분석(`app/scoring/disclosure_study.py`)의 데이터: 과거 공시, 가격, 진입일 표본.

**공시는 운영 테이블에 넣지 않는다.** `disclosure`에 과거 3개월을 넣으면 반감기가 덜 지난 사건
(예: 9/15~9/21 공시)이 다음 16:40 채점의 오버레이에 새로 섞여, 분석 때문에 운영 신호가 조용히
바뀐다. 그래서 받은 목록은 JSON 파일에 두고 거기서 읽는다. DART 호출은 운영과 같은 한도 원장을
지난다.

**가격은 `candle`에 쌓는다.** 사건 공시 종목은 대부분 추적하지 않는 종목이라 채점에 쓰이지 않는다.
실행 이름은 `STUDY_`로 시작해, 이름 앞부분으로 찾는 수집 기록 조회가 이 실행을 운영 수집으로
읽지 않게 한다. yfinance는 한 종목이 실패하면 실행 전체가 실패하므로 50종목씩 나눠 받고, 실패한
묶음은 기록하고 넘어간다.

**표본에서 빼는 것(결과를 보기 전에 정함).** 진입일 거래량이 0인 날(거래정지)과, 진입일 갭이나
시가→종가가 ±30%를 넘는 날이다. 한국 가격제한폭은 ±30%라 그 밖의 값은 권리락·병합 같은 원가격
데이터의 흔적이다. 지수 일봉이나 전날 종가가 없는 날도 잴 수 없어 뺀다. 뺀 수는 모두 보고한다.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.base import run_collector
from app.collectors.dart_disclosure import EVENT_KINDS, DartDisclosureCollector
from app.collectors.dart_fundamental import filed_date_from_receipt
from app.collectors.yfinance_history import YFinanceHistoryCollector
from app.core.calendar import Market, MarketCalendar
from app.models import Candle, Interval, MarketIndexBar
from app.models.collector import CollectorStatus
from app.repositories import instrument_repo
from app.scoring import disclosure_events
from app.scoring.disclosure_study import EventDay, strongest
from app.services import regime_service

logger = logging.getLogger(__name__)
KR = MarketCalendar(Market.KR)
SEOUL = ZoneInfo("Asia/Seoul")
RUN_PREFIX = "STUDY_"
PRICE_BATCH = 50
PRICE_LIMIT = 0.30
MAX_RANGE = timedelta(days=90)


def fetch_disclosures(start: date, end: date, path: Path) -> dict[str, Any]:
    """DART 사건 종류(B·I) 공시를 [start, end]에서 끝까지 받아 파일에 쓴다. DB에는 쓰지 않는다."""
    if end - start > MAX_RANGE:
        raise ValueError("DART list.json without a company is capped at three months")
    collector = DartDisclosureCollector()
    rows: list[dict[str, str]] = []
    calls = 0
    with httpx.Client() as client:
        for kind in EVENT_KINDS:
            page = 1
            while True:
                payload = collector._get(
                    client,
                    "list.json",
                    bgn_de=start.strftime("%Y%m%d"),
                    end_de=end.strftime("%Y%m%d"),
                    pblntf_ty=kind,
                    page_count="100",
                    page_no=str(page),
                )
                calls += 1
                items = payload.get("list") or []
                for item in items:
                    if isinstance(item, dict):
                        rows.append(
                            {
                                "corp_code": str(item.get("corp_code") or ""),
                                "rcept_no": str(item.get("rcept_no") or ""),
                                "report_nm": str(item.get("report_nm") or ""),
                                "kind": kind,
                            }
                        )
                pages = int(str(payload.get("total_page") or 1))
                if not items or page >= pages:
                    break
                page += 1
    data = {"start": start.isoformat(), "end": end.isoformat(), "calls": calls, "rows": rows}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


@dataclass(slots=True)
class Candidate:
    instrument_id: int
    filed_on: date
    entry: date
    event: disclosure_events.DisclosureEvent


def candidates(
    session: Session, data: dict[str, Any], *, first_entry: date, last_entry: date
) -> tuple[list[Candidate], Counter[str]]:
    """파일의 공시 중 마스터 종목의 사건 공시만, 진입일이 [first_entry, last_entry]인 것."""
    by_corp = {
        i.kr_corp_code: i.instrument_id
        for i in instrument_repo.list_active(
            session, asof=last_entry, market=Market.KR, tracked=None
        )
        if i.kr_corp_code
    }
    out: list[Candidate] = []
    seen: set[str] = set()
    tally: Counter[str] = Counter()
    for row in data["rows"]:
        if row["rcept_no"] in seen:
            continue
        seen.add(row["rcept_no"])
        instrument_id = by_corp.get(row["corp_code"])
        if instrument_id is None:
            tally["not in master"] += 1
            continue
        event = disclosure_events.classify(row["report_nm"])
        if event is None:
            tally["not an event"] += 1
            continue
        filed_on = filed_date_from_receipt(row["rcept_no"])
        if filed_on is None or not KR.covers(filed_on):
            tally["no filing date"] += 1
            continue
        entry = KR.next_session_open(filed_on).astimezone(SEOUL).date()
        if not first_entry <= entry <= last_entry:
            tally["entry outside the window"] += 1
            continue
        out.append(Candidate(instrument_id, filed_on, entry, event))
        tally["event"] += 1
    return out, tally


def fetch_prices(session: Session, instrument_ids: Sequence[int], *, period: str) -> list[str]:
    """일봉을 `candle`에 받는다. 실패한 묶음의 요약을 돌려준다."""
    failures: list[str] = []
    ids = sorted(set(instrument_ids))
    for start in range(0, len(ids), PRICE_BATCH):
        batch = ids[start : start + PRICE_BATCH]
        collector = YFinanceHistoryCollector(period=period, instrument_ids=batch)
        collector.name = RUN_PREFIX + collector.name
        run = run_collector(collector, session)
        if run.status not in (CollectorStatus.SUCCESS, CollectorStatus.PARTIAL):
            failures.append(f"{batch[0]}..{batch[-1]}: {run.status.value} {run.error}")
        logger.info("study prices %d/%d: %s", start + len(batch), len(ids), run.status.value)
    return failures


@dataclass(slots=True)
class Sample:
    events: list[EventDay] = field(default_factory=list)
    dropped: Counter[str] = field(default_factory=Counter)


def _daily_bars(session: Session, ids: Sequence[int], first: date, last: date) -> dict[Any, Any]:
    lo = KR.session_open(first) - timedelta(days=10)
    hi = KR.session_open(last) + timedelta(days=1)
    rows = session.execute(
        select(Candle.instrument_id, Candle.ts, Candle.open, Candle.close, Candle.volume, Candle.id)
        .where(
            Candle.instrument_id.in_(list(ids)),
            Candle.interval == Interval.DAY_1,
            Candle.ts >= lo,
            Candle.ts <= hi,
        )
        .order_by(Candle.id)
    ).all()
    # 리비전이 여럿이면 마지막(가장 새) 것이 남는다.
    return {
        (i, ts.astimezone(SEOUL).date()): (float(o), float(c), float(v))
        for i, ts, o, c, v, _ in rows
    }


def _index_bars(session: Session, first: date, last: date) -> dict[Any, Any]:
    lo = KR.session_open(first) - timedelta(days=10)
    hi = KR.session_open(last) + timedelta(days=1)
    rows = session.execute(
        select(
            MarketIndexBar.index_code, MarketIndexBar.ts, MarketIndexBar.open, MarketIndexBar.close
        )
        .where(MarketIndexBar.ts >= lo, MarketIndexBar.ts <= hi)
        .order_by(MarketIndexBar.id)
    ).all()
    return {(code, ts.astimezone(SEOUL).date()): (float(o), float(c)) for code, ts, o, c in rows}


def build_sample(
    session: Session, found: Sequence[Candidate], *, first_entry: date, last_entry: date
) -> Sample:
    """후보 공시를 진입일 표본으로. 같은 종목·같은 진입일은 가장 강한 공시 하나로 줄인다."""
    sample = Sample()
    ids = sorted({c.instrument_id for c in found})
    bars = _daily_bars(session, ids, first_entry, last_entry)
    index = _index_bars(session, first_entry, last_entry)
    instruments = {i: instrument_repo.get_by_id(session, i) for i in ids}
    grouped: dict[tuple[int, date], list[EventDay]] = {}
    for c in found:
        inst = instruments[c.instrument_id]
        if inst is None:
            sample.dropped["unknown instrument"] += 1
            continue
        code = regime_service.index_for(inst.market, inst.listing)
        previous = KR.sessions_between(c.entry - timedelta(days=14), c.entry - timedelta(days=1))[
            -1
        ]
        today = bars.get((c.instrument_id, c.entry))
        before = bars.get((c.instrument_id, previous))
        idx_today = index.get((code, c.entry))
        idx_before = index.get((code, previous))
        if today is None or before is None:
            sample.dropped["no price bar"] += 1
            continue
        if idx_today is None or idx_before is None:
            sample.dropped["no index bar"] += 1
            continue
        (o, cl, volume), prev_close = today, before[1]
        if volume <= 0 or o <= 0 or prev_close <= 0:
            sample.dropped["no trading"] += 1
            continue
        gap, oc = o / prev_close - 1, cl / o - 1
        if abs(gap) > PRICE_LIMIT or abs(oc) > PRICE_LIMIT:
            sample.dropped["beyond the price limit"] += 1
            continue
        event_day = EventDay(
            day=c.entry,
            instrument_id=c.instrument_id,
            event_type=c.event.event_type,
            sentiment=c.event.sentiment,
            intensity=c.event.intensity,
            gap=gap,
            open_close=oc,
            index_gap=idx_today[0] / idx_before[1] - 1,
            index_open_close=idx_today[1] / idx_today[0] - 1,
        )
        grouped.setdefault((c.instrument_id, c.entry), []).append(event_day)
    for members in grouped.values():
        if len(members) > 1:
            sample.dropped["merged into the strongest filing"] += len(members) - 1
        sample.events.append(strongest(members))
    return sample
