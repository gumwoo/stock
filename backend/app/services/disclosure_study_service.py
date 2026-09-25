"""공시 이벤트 분석(`app/scoring/disclosure_study.py`)의 데이터: 과거 공시, 가격, 진입일 표본.

**공시는 운영 테이블에 넣지 않는다.** `disclosure`에 과거 3개월을 넣으면 반감기가 덜 지난 사건
(예: 9/15~9/21 공시)이 다음 16:40 채점의 오버레이에 새로 섞여, 분석 때문에 운영 신호가 조용히
바뀐다. 그래서 받은 목록은 JSON 파일에 두고 거기서 읽는다. DART 호출은 운영과 같은 한도 원장을
지난다.

**가격은 `candle`에 쌓는다.** 사건 공시 종목은 대부분 추적하지 않는 종목이라 16:40 채점에는 쓰이지
않는다. 다만 장전 흐름(V2)은 풀에 든 추적 밖 종목도 08:40에 관찰용으로 채점하고, 사전 수집은 직전
거래일 종가가 이미 있는 종목을 다시 받지 않는다. 그러니 여기서 받은 일봉은 그 종목이 풀에 들면
그대로 쓰인다(6개월치. 기술 점수에 필요한 60개보다 길다). 모두 yfinance의 실제 일봉이다.
실행 이름은 `STUDY_`로 시작해, 이름 앞부분으로 찾는 수집 기록 조회가 이 실행을 운영 수집으로
읽지 않게 한다. yfinance는 한 종목이 실패하면 실행 전체가 실패하므로 50종목씩 나눠 받고, 실패한
묶음은 기록하고 넘어간다.

**표본에서 빼는 것.** 진입일 거래량이 0인 날(거래정지)과, 진입일 시가나 종가가 **전날 종가 대비**
±30%를 넘는 날이다. 가격제한폭은 전날 종가 기준이라 그 밖의 값은 권리락·병합 같은 원가격 데이터의
흔적이다. 지수 일봉이나 전날 종가가 없는 날도 잴 수 없어 뺀다. 뺀 수는 모두 보고한다.

첫 구현은 갭과 **시가→종가**에 ±30%를 걸었다. 시가 대비 움직임은 제한폭 안에서도 30%를 넘을 수
있어(하한가로 밀린 날, 상한가 갭) 실제 거래 12건을 뺐고, 정확히 +30.0%인 상한가 갭은 부동소수점
때문에 걸렸다. 검토에서 찾아 고쳤다. 판정은 바뀌지 않았다(README에 두 값 모두 있다).
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
LIMIT_SLACK = 1e-3
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
        # 가격제한폭은 전날 종가 기준이다. 정확히 ±30%인 상·하한가가 부동소수점으로
        # 걸리지 않게 조금의 여유를 둔다.
        if (
            abs(o / prev_close - 1) > PRICE_LIMIT + LIMIT_SLACK
            or abs(cl / prev_close - 1) > PRICE_LIMIT + LIMIT_SLACK
        ):
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


# --- v2: 9시~10시 1분봉 ------------------------------------------------------------
#
# 종목일마다 KIS 과거 분봉을 한 번 부른다. 10:00을 커서로 주면 그 시각부터 거꾸로 120개가 와서
# 09:00~10:00이 한 번에 들어온다(그 앞은 전날 시간외 봉이라 날짜로 거른다). 받은 봉은 파일에만
# 둔다. `minute_bar`에 한 시간만 넣으면 저녁 장중 분석이 그날을 반쪽 기록으로 읽을 수 있다.
# 호출은 운영과 같은 한도 원장과 KIS 실행 잠금을 지난다. 속도 초과 거절이 오면 멈추고, 받은
# 것까지 저장한다. 50회마다 저장하므로 끊겨도 이어서 받는다.

FIRST_HOUR_CURSOR = "100000"
SAVE_EVERY = 50


def _key(instrument_id: int, day: date) -> str:
    return f"{instrument_id}:{day.isoformat()}"


def fetch_first_hours(
    session: Session, events: Sequence[EventDay], path: Path, *, limit: int | None = None
) -> dict[str, Any]:
    """표본 종목일의 09:00~10:00 1분봉을 파일에 모은다. 이미 있는 종목일은 건너뛴다."""
    import time

    from app.collectors.base import RateLimitedError, SkipCollection, UpstreamUnavailableError
    from app.collectors.kis import KisClient
    from app.collectors.kis_minute import STOCK_PATH, STOCK_TR, kis_run_lock

    data: dict[str, Any] = (
        json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"days": {}}
    )
    done: dict[str, Any] = data["days"]
    todo = [e for e in events if _key(e.instrument_id, e.day) not in done]
    if limit is not None:
        todo = todo[:limit]
    codes = {
        e.instrument_id: instrument_repo.current_symbol(session, e.instrument_id) for e in todo
    }
    calls = 0
    try:
        with KisClient() as client:
            for e in todo:
                key = _key(e.instrument_id, e.day)
                code = codes.get(e.instrument_id)
                if not code:
                    done[key] = {"error": "no symbol"}
                    continue
                for _attempt in range(5):
                    try:
                        with kis_run_lock():
                            body, _ = client.get(
                                STOCK_PATH,
                                tr_id=STOCK_TR,
                                params={
                                    "FID_COND_MRKT_DIV_CODE": "J",
                                    "FID_INPUT_ISCD": code,
                                    "FID_INPUT_HOUR_1": FIRST_HOUR_CURSOR,
                                    "FID_INPUT_DATE_1": e.day.strftime("%Y%m%d"),
                                    "FID_PW_DATA_INCU_YN": "Y",
                                    "FID_FAKE_TICK_INCU_YN": "N",
                                },
                            )
                        break
                    except SkipCollection:
                        # 다른 KIS 실행이 돌고 있다. 두 배 속도로 부르지 않고 기다린다.
                        time.sleep(60)
                else:
                    raise SkipCollection("another KIS run kept the lock")
                calls += 1
                bars = []
                for row in body.get("output2") or []:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("stck_bsop_date")) != e.day.strftime("%Y%m%d"):
                        continue
                    label = str(row.get("stck_cntg_hour") or "")[:4]
                    if "0900" <= label <= "1000":
                        bars.append(
                            [
                                label,
                                float(row["stck_oprc"]),
                                float(row["stck_hgpr"]),
                                float(row["stck_lwpr"]),
                                float(row["stck_prpr"]),
                            ]
                        )
                done[key] = {"bars": sorted(bars)}
                if calls % SAVE_EVERY == 0:
                    path.write_text(json.dumps(data), encoding="utf-8")
                    logger.info("study first hours: %d/%d calls", calls, len(todo))
    except RateLimitedError:
        logger.error("study first hours: KIS refused the rate; stopping, progress kept")
        raise
    except UpstreamUnavailableError as exc:
        logger.error("study first hours: KIS unavailable (%s); progress kept", exc)
        raise
    finally:
        path.write_text(json.dumps(data), encoding="utf-8")
    return data


def first_hour_sample(
    events: Sequence[EventDay], minutes: dict[str, Any]
) -> tuple[list[Any], Counter[str]]:
    """v1 표본의 종목일을 첫 1시간 값으로. 봉이 없거나 09:00 봉이 없는 날은 빼고 센다."""
    from app.scoring.disclosure_first_hour import FirstHour, MinuteBar, measure

    out: list[FirstHour] = []
    dropped: Counter[str] = Counter()
    for e in events:
        got = minutes["days"].get(_key(e.instrument_id, e.day))
        if got is None:
            dropped["not fetched"] += 1
            continue
        if "error" in got:
            dropped[f"fetch: {got['error']}"] += 1
            continue
        measured = measure([MinuteBar(*b) for b in got["bars"]])
        if measured is None:
            dropped["no 09:00 bar or nothing sellable"] += 1
            continue
        ret, mfe, mae = measured
        out.append(
            FirstHour(e.day, e.instrument_id, e.event_type, e.sentiment, e.intensity, ret, mfe, mae)
        )
    return out, dropped
