"""PREOPEN_V2 아침 흐름: 시장 전체 발견 → 풀 동결 → 필요한 종목만 데이터 → LLM → 보충 → 점수 → 목록.

| 시작  | 단계                                                     | 들어가는 조건                    |
|-------|----------------------------------------------------------|----------------------------------|
| 07:00 | 체인: 전체 스윕 → 풀 확정 → 검색 추세 → 사전 수집 → LLM  | 거래일                           |
| 08:30 | 보충 스윕 → 보충 기사 LLM                                | 풀 확정, 07:00 LLM 단계 종료     |
| 08:40 | 그날 관찰용 점수                                         | 풀 확정, 사전 수집 단계 종료     |
| 08:50 | V2 목록 고정                                             | 없음. 빠진 단계는 기록만 한다    |

**시각은 시작 신호일 뿐이다.** 뒷 단계는 앞 단계가 끝났는지를 `preopen_pool.stages`로
확인하고, 안 끝났으면 1분마다 다시 본다. 08:45까지도 안 되면 건너뛰었다고 기록한다.
08:50 목록만은 무엇이 빠졌든 만든다. 늦은 것은 목록이 없는 것보다 낫고, 무엇이
빠졌는지는 목록의 `inputs`에 남는다. 풀조차 없으면 `DEGRADED_FALLBACK` 풀을 그
자리에서 만들어 같은 계보로 잇는다.

**발굴 점수는 07:00 값으로 얼린다.** 08:50에 발굴을 다시 돌리면 08:30 보충 스윕이
넣은 기사가 풀 종목에만 더해져, 풀 종목의 급증이 부풀려진다. 목록의
`DISCOVERY_SURGE`는 풀에 얼린 값만 본다. 보충 기사는 뉴스 점수(LLM 해석)로만 들어간다.

**사전 수집은 "있느냐"가 아니라 "쓸 만큼 최신이냐"로 판단한다.** 가격은 직전
거래일 종가가 있어야 최신이다. 재무는 그 종목으로 DART에 마지막으로 닿은 지
7일(기존 재무 신선도 기준과 같은 값)이 지났으면 다시 받는다. 반기·분기 보고서는
보지 않는다. 재무 수집기가 사업보고서만 읽어서, 새로 받아도 점수가 바뀌지 않는다.
실행 이름은 `PREFETCH_`로 시작한다. `DART…`로 남기면 몇 종목짜리 실행이 추적 종목
전체의 재무 신선도 판정(`last_success("DART")`, 이름 앞부분 일치)을 속인다.
추적 등록(`tracked`)은 하지 않는다. 하루 상한을 넘은 종목은 `SKIPPED_CAP`으로 남고,
점수가 없다는 이유로 목록에서 빠지지 않는다.

**점수는 참고용이고 `signal`에 쓰지 않는다.** 기존 엔진을 그대로 쓰되, 비교군은
그 시점의 추적 종목으로 고정하고 수와 해시를 남긴다. 시점은 셋으로 나눠 남긴다.
기술 데이터는 직전 거래일 종가, 재무 데이터는 점수가 쓸 수 있던 가장 늦은 공시가
쓸 수 있게 된 시각, 평가는 08:40이다. 엔진은 재무를 가격 일봉과 같은 순간(직전
종가)에 맞춰 읽는다. 공시의 `available_at`은 다음 개장이라 전날 장 마감 뒤 공시는
어차피 09:00에야 쓸 수 있으므로, 08:40까지 쓸 수 있는 재무와 같은 집합이다.

**한 단계의 예상 밖 오류는 그 단계의 FAILED로 남고 체인은 계속된다.** 다른 작업은
프로그래밍 오류를 그대로 터뜨려 드러내지만, 아침 체인이 멈추면 그날 목록의 입력이
통째로 빠진다. 오류는 추적 정보와 함께 로그에 남고, 단계 상태에도 남는다.
"""

from __future__ import annotations

import hashlib
import logging
import time as time_module
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collectors.base import run_collector
from app.collectors.dart_disclosure import DartDisclosureCollector
from app.collectors.dart_fundamental import DartFundamentalCollector
from app.collectors.naver_datalab import NaverDataLabCollector
from app.collectors.naver_news import NaverNewsCollector
from app.collectors.preopen_news import PreopenNewsSupplement
from app.collectors.quota import QuotaGuard
from app.collectors.yfinance_history import YFinanceHistoryCollector
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import ensure_utc, utc_now
from app.core.types import Engine
from app.models import CollectorRun, Instrument, Interval
from app.models.collector import CollectorStatus
from app.models.fundamental import FundamentalSource
from app.models.llm import LlmCall
from app.models.news import NewsSource, NewsSweepCoverage
from app.models.preopen import DEGRADED_FALLBACK, NORMAL, PreopenPool, PreopenPoolMember
from app.models.watchlist import WatchlistMember, WatchlistSnapshot
from app.repositories import (
    candle_repo,
    disclosure_repo,
    fundamental_repo,
    instrument_repo,
    news_repo,
    promotion_repo,
)
from app.scoring import disclosure_events
from app.scoring.watchlist import (
    SELECTION_VERSION_V2,
    STRATEGY_VERSION_V2,
    Seen,
    Selection,
    select_names_v2,
)
from app.services import (
    attention_service,
    discovery_service,
    llm_service,
    overlay_service,
    regime_service,
    scoring_service,
    watchlist_service,
)

logger = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")
KR = MarketCalendar(Market.KR)

# 아침의 시작. 이 시각 뒤에 돈 입력만 "오늘 아침" 입력으로 센다.
MORNING = time(7, 0)
# 08:30·08:40 단계가 앞 단계를 기다리는 한계. 08:50 목록 전에 끝나야 한다.
DEADLINE = time(8, 45)
POLL = timedelta(seconds=60)

DISCOVERY_TOP = 30
PREFETCH_CAP = 20
LLM_MORNING_BUDGET = 100
LLM_SUPPLEMENT_BUDGET = 30
# 재무를 다시 받는 간격. 기존 재무 신선도 기준과 같은 값을 쓴다.
FUNDAMENTAL_REFRESH = scoring_service.FUNDAMENTAL_SOURCE_CHECK
PREFETCH_PREFIX = "PREFETCH_"
NEW_PRICES_PERIOD = "5y"
REFRESH_PRICES_PERIOD = "1mo"
NEW_FUNDAMENTAL_YEARS = 5
REFRESH_FUNDAMENTAL_YEARS = 2

# 단계 이름
SWEEP = "sweep"
POOL = "pool"
SEARCH_TRENDS = "search_trends"
PREFETCH = "prefetch"
LLM = "llm"
SUPPLEMENT = "supplement"
SUPPLEMENT_LLM = "supplement_llm"
SCORE = "score"
SNAPSHOT = "snapshot"

RUNNING = "RUNNING"
SUCCESS = "SUCCESS"
PARTIAL = "PARTIAL"
FAILED = "FAILED"
SKIPPED = "SKIPPED"
FINISHED = frozenset({SUCCESS, PARTIAL, FAILED, SKIPPED})

# 사전 수집 상태
FETCHED = "FETCHED"
FRESH = "FRESH"
SKIPPED_CAP = "SKIPPED_CAP"
NO_DATA = "NO_DATA"

# 풀에 들어온 경로
FROM_DISCOVERY = "DISCOVERY"
FROM_DISCLOSURE = "DISCLOSURE"
FROM_FOCUS = "FOCUS"

_GOT = frozenset({CollectorStatus.SUCCESS, CollectorStatus.PARTIAL})


# --- 풀과 단계 상태 ---------------------------------------------------------------


def pool_for(session: Session, day: Any) -> PreopenPool | None:
    return session.execute(
        select(PreopenPool).where(PreopenPool.session_date == day)
    ).scalar_one_or_none()


def members_of(session: Session, pool: PreopenPool) -> list[PreopenPoolMember]:
    return list(
        session.execute(
            select(PreopenPoolMember)
            .where(PreopenPoolMember.pool_id == pool.id)
            .order_by(PreopenPoolMember.instrument_id)
        ).scalars()
    )


def stage_status(pool: PreopenPool | None, name: str) -> str | None:
    if pool is None:
        return None
    entry = pool.stages.get(name)
    return str(entry["status"]) if isinstance(entry, dict) else None


def _locked_stages(session: Session, pool: PreopenPool) -> dict[str, object]:
    """풀 행을 잠그고 DB의 최신 `stages`를 읽는다. 커밋할 때 잠금이 풀린다.

    07:00 체인, 08:30 보충, 08:40 점수, 08:50 목록은 서로 다른 세션에서 같은
    행을 쓴다. 세션은 커밋 뒤에도 객체를 만료시키지 않으므로(`expire_on_commit
    =False`), 처음 읽은 사본에 덧써서 칸 전체를 저장하면 다른 세션이 그사이 적은
    단계가 지워진다. 실제로 끝난 LLM 단계가 RUNNING으로 되돌아가 08:30 보충이
    기다리다 건너뛰는 순서를 재현했다. 그래서 쓰기 직전에 잠그고 다시 읽는다.
    잠그기 전에 커밋하지 않은 풀 변경은 버려지므로, 부르는 쪽이 먼저 커밋한다.
    """
    session.refresh(pool, with_for_update=True)
    return dict(pool.stages)


def _mark(
    session: Session,
    pool: PreopenPool,
    name: str,
    status: str,
    *,
    detail: str | None = None,
    **extra: object,
) -> None:
    """한 단계의 상태를 적고 커밋한다. JSON 칸이라 사전을 새로 만들어 넣는다."""
    now = utc_now().isoformat()
    stages = _locked_stages(session, pool)
    raw = stages.get(name)
    entry: dict[str, object] = dict(raw) if isinstance(raw, dict) else {}
    if status == RUNNING:
        entry = {"started_at": now}
    else:
        entry.setdefault("started_at", now)
        entry["finished_at"] = now
    entry["status"] = status
    if detail is not None:
        entry["detail"] = detail[:1000]
    entry.update({k: v.isoformat() if isinstance(v, datetime) else v for k, v in extra.items()})
    stages[name] = entry
    pool.stages = stages
    session.commit()


def _note(session: Session, pool: PreopenPool, name: str, **extra: object) -> None:
    """단계 상태는 그대로 두고 값만 덧붙인다."""
    stages = _locked_stages(session, pool)
    raw = stages.get(name)
    entry: dict[str, object] = dict(raw) if isinstance(raw, dict) else {}
    entry.update({k: v.isoformat() if isinstance(v, datetime) else v for k, v in extra.items()})
    stages[name] = entry
    pool.stages = stages
    session.commit()


def _step(session: Session, pool: PreopenPool, name: str, fn: Callable[[], tuple[str, str]]) -> str:
    """한 단계를 돌리고 상태를 남긴다. 예상 밖 오류도 FAILED로 남기고 넘어간다."""
    _mark(session, pool, name, RUNNING)
    try:
        status, detail = fn()
    except Exception as exc:  # 아침 체인은 한 단계 때문에 멈추지 않는다
        session.rollback()
        logger.exception("preopen %s: %s failed", pool.session_date, name)
        _mark(session, pool, name, FAILED, detail=f"{type(exc).__name__}: {exc}")
        return FAILED
    _mark(session, pool, name, status, detail=detail)
    logger.info("preopen %s: %s %s — %s", pool.session_date, name, status, detail)
    return status


def _run_status(run: CollectorRun) -> str:
    return {
        CollectorStatus.SUCCESS: SUCCESS,
        CollectorStatus.PARTIAL: PARTIAL,
        CollectorStatus.SKIPPED: SKIPPED,
    }.get(run.status, FAILED)


def _before_open(now: datetime) -> Any | None:
    """오늘이 거래일이고 아직 개장 전이면 그 날짜. 아니면 None."""
    day = KR.local_today(now)
    if not KR.is_session(day) or now >= KR.session_open(day):
        return None
    return day


def _previous_session(day: Any) -> Any:
    return KR.sessions_between(day - timedelta(days=14), day - timedelta(days=1))[-1]


def event_intensity(filed: Sequence[Any]) -> dict[int, float]:
    """종목마다 사건 공시 중 가장 강한 것의 강도. 사건이 아닌 공시는 세지 않는다."""
    out: dict[int, float] = {}
    for d in filed:
        event = disclosure_events.classify(d.report_nm)
        if event is not None:
            out[d.instrument_id] = max(out.get(d.instrument_id, 0.0), event.intensity)
    return out


# --- 풀 확정 -----------------------------------------------------------------------


def freeze(
    session: Session,
    pool: PreopenPool,
    *,
    asof: datetime,
    only: Collection[int] | None = None,
) -> tuple[str, str]:
    """`asof` 시점의 후보 풀을 얼린다: 발굴 상위 30, 사건 공시, 관심 종목.

    관심 종목(추적 종목과 최근 3일 후보)은 뉴스·검색 이유를 보려고 넣을 뿐,
    풀에 있다고 목록에 들어가지는 않는다. 모두 `asof`에 기록돼 있던 것만 읽는다.
    `only`는 테스트가 풀을 자기 종목으로 좁히는 데 쓴다.

    이미 얼린 풀은 다시 얼리지 않는다. 07:00 체인이 늦어 08:50 목록이 먼저
    얼렸다면, 뒤늦게 온 체인은 그 풀을 그대로 쓴다.
    """
    # 잠가서 읽는다. 늦은 07:00 체인과 08:50 대체 풀이 동시에 얼리려 하면 뒤에
    # 온 쪽이 앞의 커밋을 기다렸다가 이미 얼린 것을 본다.
    session.refresh(pool, with_for_update=True)
    if pool.asof is not None:
        session.commit()
        return SKIPPED, f"already frozen at {pool.asof:%H:%M}Z"
    day = pool.session_date
    korean = {
        i.instrument_id: i
        for i in instrument_repo.list_active(session, asof=day, market=Market.KR, tracked=None)
    }
    later = promotion_repo.promoted_after(session, asof)
    tracked = {i for i, inst in korean.items() if inst.tracked and i not in later}

    found = discovery_service.discover(session, asof=asof, top=DISCOVERY_TOP)
    discovery = {c.instrument_id: c.score for c in found.candidates}

    filed = disclosure_repo.filed_between(
        session,
        first=_previous_session(day),
        before=day,
        stored_by=asof,
        instrument_ids=list(korean),
    )
    intensity = event_intensity(filed)
    with_event = set(intensity)
    focus = set(llm_service.focus_ids_asof(session, asof))

    ids = (set(discovery) | with_event | focus) & set(korean)
    if only is not None:
        ids &= set(only)
    for i in sorted(ids):
        sources = [
            name
            for name, present in (
                (FROM_DISCOVERY, i in discovery),
                (FROM_DISCLOSURE, i in with_event),
                (FROM_FOCUS, i in focus),
            )
            if present
        ]
        session.add(
            PreopenPoolMember(
                pool_id=pool.id,
                instrument_id=i,
                sources=sources,
                tracked=i in tracked,
                discovery_score=discovery.get(i),
                has_disclosure_event=i in with_event,
                disclosure_intensity=intensity.get(i),
            )
        )
    pool.asof = asof
    pool.pool_count = len(ids)
    pool.discovery = {
        "asof": asof.isoformat(),
        "top": DISCOVERY_TOP,
        "freshness": found.freshness.value,
        "considered": found.considered,
        "unmeasured": found.unmeasured,
        "candidates": len(found.candidates),
    }
    session.commit()
    detail = (
        f"{len(ids)} names: {len(discovery)} discovered, {len(with_event)} with an event "
        f"disclosure, {len(focus & set(korean))} in focus; news {found.freshness.value}"
    )
    return SUCCESS, detail


# --- 사전 수집 ---------------------------------------------------------------------

Fetch = Callable[..., CollectorRun]


def fetch_prices(session: Session, instrument_ids: Sequence[int], *, period: str) -> CollectorRun:
    collector = YFinanceHistoryCollector(period=period, instrument_ids=instrument_ids)
    collector.name = PREFETCH_PREFIX + collector.name
    return run_collector(collector, session)


def fetch_fundamentals(
    session: Session, instrument_ids: Sequence[int], *, years_back: int
) -> CollectorRun:
    collector = DartFundamentalCollector(years_back=years_back, instrument_ids=instrument_ids)
    collector.name = PREFETCH_PREFIX + collector.name
    return run_collector(collector, session)


def _latest_bar_at(session: Session, instrument_id: int, now: datetime) -> datetime | None:
    bars = candle_repo.history(
        session, instrument_id, Interval.DAY_1, limit=1, available_before=now
    )
    return bars[-1].available_at if bars else None


def tracked_fundamentals_checked(session: Session) -> datetime | None:
    """추적 종목의 재무를 마지막으로 받은 시각: `DART_FUNDAMENTAL` 실행만, 이름 그대로.

    기존 `last_success("DART")`는 이름을 `LIKE 'DART%'`로 찾아 공시 수집
    (`DART_DISCLOSURE`)까지 센다. 07:00 체인이 매일 아침 공시를 받으므로, 그걸로
    읽으면 추적 종목의 재무가 늘 "오늘 확인함"으로 보인다(2026-09-25 확인: 공시
    수집 06:33Z가 답이었고, 실제 마지막 재무 실행은 9/22). 16:40 채점은 아직 그
    함수를 쓴다. 여기서는 정확한 이름으로만 본다.
    """
    return session.execute(
        select(func.max(CollectorRun.finished_at)).where(
            CollectorRun.source == DartFundamentalCollector.name,
            CollectorRun.status.in_(list(_GOT)),
        )
    ).scalar()


def last_fundamental_check(session: Session, instrument_id: int) -> datetime | None:
    """이 종목으로 DART에 마지막으로 닿은 시각. 지난 아침들의 풀 기록에서 읽는다."""
    return session.execute(
        select(func.max(PreopenPoolMember.fundamental_checked_at)).where(
            PreopenPoolMember.instrument_id == instrument_id
        )
    ).scalar()


def provisional_ranks(
    session: Session, members: Sequence[PreopenPoolMember], *, asof: datetime
) -> Selection:
    """뉴스 점수 없이 매긴 임시 V2 순위: 발굴·공시·검색 급증만으로."""
    seen = []
    for m in members:
        attention, _ = attention_service.attention_at(session, m.instrument_id, asof)
        seen.append(
            Seen(
                instrument_id=m.instrument_id,
                tracked=m.tracked,
                has_disclosure_event=m.has_disclosure_event,
                disclosure_intensity=m.disclosure_intensity,
                search_surge=attention.surge,
                discovery_score=m.discovery_score,
            )
        )
    return select_names_v2(seen, limit=len(seen))


def prefetch(
    session: Session,
    pool: PreopenPool,
    *,
    now: datetime,
    cap: int = PREFETCH_CAP,
    prices: Fetch = fetch_prices,
    fundamentals: Fetch = fetch_fundamentals,
) -> tuple[str, str]:
    """임시 순위가 높은 종목부터, 쓸 만큼 최신이 아닌 것만 하루 `cap`종목까지 받는다."""
    members = members_of(session, pool)
    ranks = {p.instrument_id: p.rank for p in provisional_ranks(session, members, asof=now).picks}
    for m in members:
        m.provisional_rank = ranks.get(m.instrument_id)
    session.commit()
    ordered = sorted(
        members,
        key=lambda m: (m.provisional_rank is None, m.provisional_rank or 0, m.instrument_id),
    )

    previous_close = KR.session_close(_previous_session(KR.local_today(now)))
    dart_checked = tracked_fundamentals_checked(session)
    fetched = 0
    counts: Counter[str] = Counter()
    for m in ordered:
        inst = session.get(Instrument, m.instrument_id)
        assert inst is not None
        bar_at = _latest_bar_at(session, m.instrument_id, now)
        need_price = bar_at is None or bar_at < previous_close
        # 추적 종목의 재무는 16:40 전체 DART 실행이 챙긴다.
        checked = dart_checked if m.tracked else last_fundamental_check(session, m.instrument_id)
        need_fundamentals = (
            not m.tracked
            and bool(inst.kr_corp_code)
            and (
                checked is None or now - ensure_utc(checked, field="checked") > FUNDAMENTAL_REFRESH
            )
        )
        m.fundamental_checked_at = checked
        if not need_price and not need_fundamentals:
            m.prefetch_status = FRESH
        elif fetched >= cap:
            m.prefetch_status = SKIPPED_CAP
        else:
            fetched += 1
            m.prefetch_status = _fetch_one(
                session,
                m,
                now=now,
                need_price=need_price,
                had_bars=bar_at is not None,
                need_fundamentals=need_fundamentals,
                prices=prices,
                fundamentals=fundamentals,
            )
        counts[m.prefetch_status] += 1
        session.commit()

    detail = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no names"
    status = PARTIAL if counts[FAILED] or counts[SKIPPED_CAP] else SUCCESS
    return status, detail


def _fetch_one(
    session: Session,
    m: PreopenPoolMember,
    *,
    now: datetime,
    need_price: bool,
    had_bars: bool,
    need_fundamentals: bool,
    prices: Fetch,
    fundamentals: Fetch,
) -> str:
    ok = True
    try:
        if need_price:
            run = prices(
                session,
                [m.instrument_id],
                period=REFRESH_PRICES_PERIOD if had_bars else NEW_PRICES_PERIOD,
            )
            ok = run.status in _GOT
        if need_fundamentals:
            has_facts = fundamental_repo.count_for(session, m.instrument_id) > 0
            run = fundamentals(
                session,
                [m.instrument_id],
                years_back=REFRESH_FUNDAMENTAL_YEARS if has_facts else NEW_FUNDAMENTAL_YEARS,
            )
            if run.status in _GOT:
                m.fundamental_checked_at = run.finished_at
            else:
                ok = False
    except Exception:  # 한 종목의 실패는 그 종목의 FAILED다
        session.rollback()
        logger.exception("preopen prefetch: instrument %s", m.instrument_id)
        return FAILED
    if _latest_bar_at(session, m.instrument_id, now) is None:
        return NO_DATA
    return FETCHED if ok else FAILED


# --- 08:40 관찰용 점수 ---------------------------------------------------------------


def score_pool(session: Session, pool: PreopenPool, *, now: datetime) -> tuple[str, str]:
    """풀 종목마다 그날 관찰용 점수를 계산해 풀 행에 남긴다. `signal`에는 쓰지 않는다."""
    later = promotion_repo.promoted_after(session, now)
    peers = [
        i
        for i in instrument_repo.list_active(
            session, asof=pool.session_date, market=Market.KR, tracked=True
        )
        if i.instrument_id not in later
    ]
    lookup = scoring_service.market_peer_lookup(session, peers)
    peer_ids = sorted(i.instrument_id for i in peers)
    peer_hash = hashlib.sha256(",".join(map(str, peer_ids)).encode()).hexdigest()
    dart_checked = tracked_fundamentals_checked(session)

    scored_n = no_bars = failed = 0
    for m in members_of(session, pool):
        inst = session.get(Instrument, m.instrument_id)
        assert inst is not None
        m.evaluated_at = now
        m.peer_count = len(peer_ids)
        m.peer_hash = peer_hash
        checked = dart_checked if m.tracked else m.fundamental_checked_at
        try:
            scored = scoring_service.score_instrument(
                session, inst, now=now, peers=lookup, fundamental_checked_at=checked
            )
        except Exception as exc:  # 한 종목 때문에 나머지 점수를 잃지 않는다
            logger.exception("preopen score: instrument %s", m.instrument_id)
            session.rollback()
            m.evaluated_at = now
            m.abstained_reason = f"점수 계산 실패: {type(exc).__name__}"
            session.commit()
            failed += 1
            continue
        if scored is None:
            m.abstained_reason = "일봉이 없어 점수를 낼 수 없음"
            session.commit()
            no_bars += 1
            continue
        by_engine = {f.engine: f for f in scored.factors}
        technical = by_engine.get(Engine.TECHNICAL)
        fundamental = by_engine.get(Engine.FUNDAMENTAL)
        m.total_score = scored.total_score
        m.technical_score = technical.score if technical else None
        m.fundamental_score = fundamental.score if fundamental else None
        m.action = scored.action.value
        m.price_data_asof = scored.data_asof
        instants = [
            t
            for t in fundamental_repo.available_instants(
                session, m.instrument_id, source=FundamentalSource.DART
            )
            if t <= scored.data_asof
        ]
        m.fundamental_data_asof = max(instants) if instants else None
        notes = [scored.abstained_reason] + [
            f.availability_reason for f in scored.factors if f.effective_weight == 0.0
        ]
        m.abstained_reason = " / ".join(n for n in notes if n) or None
        session.commit()
        scored_n += 1
    detail = f"{scored_n} scored, {no_bars} without bars, {failed} failed; {len(peer_ids)} peers"
    return (PARTIAL if failed else SUCCESS), detail


# --- 작업: 07:00 체인, 08:30 보충, 08:40 점수 ------------------------------------


def run_morning(session: Session, *, clock: Callable[[], datetime] = utc_now) -> PreopenPool | None:
    """07:00 체인. 한 작업 안에서 순서대로 돌아, 앞 단계가 끝나야 다음이 시작한다."""
    now = clock()
    day = _before_open(now)
    if day is None:
        logger.info("preopen: no morning chain at %s (not a session, or past the open)", now)
        return None
    if pool_for(session, day) is not None:
        logger.info("preopen: %s already has a pool", day)
        return None
    pool = PreopenPool(
        session_date=day, status=NORMAL, started_at=now, discovery={}, stages={}, pool_count=0
    )
    session.add(pool)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return None

    # 다 쓴 한도 버킷은 아침 체인이 치운다. 예전 08:00 뉴스 작업이 하던 일이다.
    # 청소가 실패해도 아침은 간다. 원장 판단은 창 밖 버킷을 세지 않는다.
    try:
        QuotaGuard().prune()
    except Exception:  # 청소 실패로 아침 체인을 멈추지 않는다
        session.rollback()
        logger.exception("preopen: pruning the quota ledger failed")

    def sweep() -> tuple[str, str]:
        news = run_collector(NaverNewsCollector(), session)
        disclosures = run_collector(DartDisclosureCollector(), session)
        # 보충 스윕은 이 시각 뒤만 읽는다.
        _note(session, pool, SWEEP, news_started_at=news.started_at)
        return _run_status(
            news
        ), f"news {news.status.value}, disclosures {disclosures.status.value}"

    _step(session, pool, SWEEP, sweep)
    if _stop(session, pool, day, clock, (POOL, SEARCH_TRENDS, PREFETCH, LLM)):
        return pool
    _step(session, pool, POOL, lambda: freeze(session, pool, asof=clock()))
    if pool.asof is None:
        for name in (SEARCH_TRENDS, PREFETCH, LLM):
            _mark(session, pool, name, SKIPPED, detail="pool was not frozen")
        return pool
    ids = [m.instrument_id for m in members_of(session, pool)]

    def trends() -> tuple[str, str]:
        run = run_collector(NaverDataLabCollector(instrument_ids=ids), session)
        return _run_status(run), f"{len(ids)} names, {run.status.value}"

    if _stop(session, pool, day, clock, (SEARCH_TRENDS, PREFETCH, LLM)):
        return pool
    _step(session, pool, SEARCH_TRENDS, trends)
    if _stop(session, pool, day, clock, (PREFETCH, LLM)):
        return pool
    _step(session, pool, PREFETCH, lambda: prefetch(session, pool, now=clock()))
    if _stop(session, pool, day, clock, (LLM,)):
        return pool
    _step(session, pool, LLM, lambda: _read(session, ids, LLM_MORNING_BUDGET, after_hit_id=None))
    return pool


def _stop(
    session: Session,
    pool: PreopenPool,
    day: Any,
    clock: Callable[[], datetime],
    remaining: Sequence[str],
) -> bool:
    """개장했거나 오늘 V2 목록이 이미 있으면, 남은 단계를 건너뛰었다고 적고 멈춘다.

    늦게 깬 체인이 08:50 뒤에도 사전 수집과 LLM 100건을 계속 돌면, 이미 얼린
    목록이 가리키는 풀 행을 다시 쓰고 아무도 읽지 않을 해석에 구독을 쓴다.
    """
    listed = session.execute(
        select(WatchlistSnapshot.id).where(
            WatchlistSnapshot.session_date == day,
            WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
        )
    ).first()
    now = clock()
    if listed is None and now < KR.session_open(day):
        return False
    why = "the list is already frozen" if listed is not None else "past the open"
    session.refresh(pool)
    for name in remaining:
        # 08:50 대체 풀이 이미 끝낸 단계(예: 풀 확정)는 그 기록을 그대로 둔다.
        if stage_status(pool, name) not in FINISHED:
            _mark(session, pool, name, SKIPPED, detail=why)
    return True


def _read(
    session: Session, ids: Sequence[int], budget: int, *, after_hit_id: int | None
) -> tuple[str, str]:
    if not get_settings().llm_schedule_enabled:
        return SKIPPED, "LLM_SCHEDULE_ENABLED is off"
    run = llm_service.run_within_budget(
        session, budget=budget, instrument_ids=ids, after_hit_id=after_hit_id
    )
    detail = f"{run.items} of {budget} items"
    if run.stopped:
        detail += f"; stopped: {run.stopped}"
    if run.stopped is None:
        return SUCCESS, detail
    return (PARTIAL if run.items else SKIPPED), detail


def _wait_for(
    session: Session,
    day: Any,
    needs: Sequence[str],
    *,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> tuple[PreopenPool | None, str | None]:
    """풀이 확정되고 `needs` 단계가 모두 끝날 때까지 기다린다. 08:45가 한계다."""
    deadline = datetime.combine(day, DEADLINE, tzinfo=SEOUL)
    while True:
        session.expire_all()
        pool = pool_for(session, day)
        waiting = []
        if pool is None or pool.asof is None:
            waiting.append(POOL)
        waiting += [n for n in needs if stage_status(pool, n) not in FINISHED]
        if not waiting:
            return pool, None
        if clock() >= deadline:
            return pool, f"prerequisite not finished by {DEADLINE:%H:%M}: {', '.join(waiting)}"
        sleep(POLL.total_seconds())


def run_supplement(
    session: Session,
    *,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time_module.sleep,
) -> PreopenPool | None:
    """08:30: 풀 확정과 07:00 LLM 종료를 확인한 뒤, 풀 종목의 아침 기사만 보충한다."""
    day = _before_open(clock())
    if day is None:
        return None
    pool, why = _wait_for(session, day, (LLM,), clock=clock, sleep=sleep)
    if pool is None:
        logger.warning("preopen supplement: no pool for %s", day)
        return None
    if why is not None:
        for name in (SUPPLEMENT, SUPPLEMENT_LLM):
            _mark(session, pool, name, SKIPPED, detail=why)
        return pool
    ids = [m.instrument_id for m in members_of(session, pool)]
    sweep = pool.stages.get(SWEEP) or {}
    since_raw = sweep.get("news_started_at") if isinstance(sweep, dict) else None
    since = datetime.fromisoformat(str(since_raw)) if since_raw else pool.started_at
    # 보충이 새로 만든 종목-기사 쌍만 LLM에 넘기기 위한 경계.
    mark = news_repo.last_hit_id(session)

    def supplement() -> tuple[str, str]:
        run = run_collector(PreopenNewsSupplement(instrument_ids=ids, since=since), session)
        return _run_status(run), f"{len(ids)} names since {since:%H:%M}Z, {run.status.value}"

    _step(session, pool, SUPPLEMENT, supplement)
    _step(
        session,
        pool,
        SUPPLEMENT_LLM,
        lambda: _read(session, ids, LLM_SUPPLEMENT_BUDGET, after_hit_id=mark),
    )
    return pool


def run_scores(
    session: Session,
    *,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time_module.sleep,
) -> PreopenPool | None:
    """08:40: 풀 확정과 사전 수집 종료를 확인한 뒤, 풀 종목의 관찰용 점수를 계산한다."""
    day = _before_open(clock())
    if day is None:
        return None
    pool, why = _wait_for(session, day, (PREFETCH,), clock=clock, sleep=sleep)
    if pool is None:
        logger.warning("preopen scores: no pool for %s", day)
        return None
    if why is not None:
        _mark(session, pool, SCORE, SKIPPED, detail=why)
        return pool
    _step(session, pool, SCORE, lambda: score_pool(session, pool, now=clock()))
    return pool


# --- 08:50 V2 목록 -------------------------------------------------------------------


def _swept(session: Session, ids: list[int], asof: datetime) -> dict[int, datetime]:
    """종목마다 `asof`까지 기록된 가장 최근 뉴스 스윕. 전체 스윕과 보충 둘 다."""
    c = NewsSweepCoverage
    rows = session.execute(
        select(c.instrument_id, func.max(c.covered_to))
        .where(
            c.instrument_id.in_(ids),
            c.source == NewsSource.NAVER_NEWS,
            c.collector.in_([NaverNewsCollector.name, PreopenNewsSupplement.name]),
            c.recorded_at <= asof,
        )
        .group_by(c.instrument_id)
    ).all()
    return {i: t for i, t in rows}  # noqa: C416 - rows, not pairs


def _inputs(
    session: Session,
    pool: PreopenPool,
    members: Sequence[PreopenPoolMember],
    picked: list[int],
    swept: dict[int, datetime],
    *,
    asof: datetime,
    morning: datetime,
) -> dict[str, object]:
    def last_run(source: str) -> str | None:
        status = session.execute(
            select(CollectorRun.status)
            .where(
                CollectorRun.source == source,
                CollectorRun.started_at >= morning,
                CollectorRun.started_at <= asof,
            )
            .order_by(CollectorRun.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return None if status is None else str(status.value)

    calls = session.execute(
        select(LlmCall.status, func.count())
        .where(LlmCall.called_at >= morning, LlmCall.called_at <= asof)
        .group_by(LlmCall.status)
    ).all()
    by_status = {str(s): int(n) for s, n in calls}
    return {
        "news": {
            "run": last_run(NaverNewsCollector.name),
            "supplement": last_run(PreopenNewsSupplement.name),
            "members": len(picked),
            "swept_this_morning": sum(1 for i in picked if swept.get(i, morning) > morning),
        },
        "llm": {"calls": sum(by_status.values()), "by_status": by_status},
        "search_trends": last_run("NAVER_DATALAB"),
        "disclosures": last_run("DART_DISCLOSURE"),
        "pool": {
            "id": pool.id,
            "status": pool.status,
            "pool_count": pool.pool_count,
            "stages": {
                k: v.get("status") if isinstance(v, dict) else None for k, v in pool.stages.items()
            },
        },
        "prefetch": dict(Counter(m.prefetch_status or "NONE" for m in members)),
    }


def take_snapshot(
    session: Session,
    *,
    now: datetime | None = None,
    only: Collection[int] | None = None,
) -> WatchlistSnapshot | None:
    """08:50 V2 목록. 거래일이 아니거나, 개장 뒤거나, 이미 있으면 None.

    풀이 없거나 아직 얼지 않았으면 `DEGRADED_FALLBACK`으로 그 자리에서 얼린다.
    `only`는 테스트가 풀을 자기 종목으로 좁히는 데 쓴다. 운영에서는 넘기지 않는다.
    """
    asof = ensure_utc(now, field="now") if now is not None else utc_now()
    day = _before_open(asof)
    if day is None:
        logger.info("watchlist v2: no list at %s (not a session, or past the open)", asof)
        return None
    already = session.execute(
        select(WatchlistSnapshot.id).where(
            WatchlistSnapshot.session_date == day,
            WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
        )
    ).first()
    if already is not None:
        logger.info("watchlist v2: %s already has a list", day)
        return None

    pool = pool_for(session, day)
    if pool is None:
        pool = PreopenPool(
            session_date=day,
            status=DEGRADED_FALLBACK,
            started_at=asof,
            discovery={},
            stages={},
            pool_count=0,
        )
        session.add(pool)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            pool = pool_for(session, day)
            assert pool is not None
    # 잠가서 다시 읽고 판단한다. 07:00 체인이 지금 얼리는 중이면 그 커밋을
    # 기다렸다가 정상으로 얼린 풀을 그대로 쓴다. 잠그지 않고 읽은 값으로 판단하면
    # 체인이 정상으로 얼린 풀을 DEGRADED로 잘못 표시한다.
    session.refresh(pool, with_for_update=True)
    if pool.asof is not None:
        session.commit()
    else:
        # 07:00 체인이 풀을 얼리지 못했다. 지금 얼리고, 그렇게 됐다고 남긴다.
        pool.status = DEGRADED_FALLBACK
        session.commit()
        _step(session, pool, POOL, lambda: freeze(session, pool, asof=asof, only=only))
        session.refresh(pool)
        if pool.asof is None:
            # 풀조차 못 얼렸다. 0개 목록을 만들면 화면이 장애를 "오늘 조건에 맞는
            # 종목 없음"으로 보여 준다. 목록을 만들지 않아야 "목록 없음(생성
            # 실패)"로 보인다.
            logger.error("watchlist v2: %s pool could not be frozen; no list", day)
            return None

    members = [m for m in members_of(session, pool) if only is None or m.instrument_id in only]
    by_id = {m.instrument_id: m for m in members}
    ids = sorted(by_id)
    overlays = overlay_service.overlays_at(session, asof=asof, instrument_ids=ids)
    attention: dict[int, tuple[str, float | None]] = {}
    seen = []
    for i in ids:
        found, _ = attention_service.attention_at(session, i, asof)
        attention[i] = (found.status, found.surge)
        m = by_id[i]
        seen.append(
            Seen(
                instrument_id=i,
                tracked=m.tracked,
                overlay_points=overlays[i].overlay.points if i in overlays else None,
                has_disclosure_event=m.has_disclosure_event,
                disclosure_intensity=m.disclosure_intensity,
                search_surge=found.surge,
                discovery_score=m.discovery_score,
            )
        )
    chosen = select_names_v2(seen)
    picked = [p.instrument_id for p in chosen.picks]
    swept = _swept(session, ids, asof)
    morning = datetime.combine(day, MORNING, tzinfo=SEOUL)

    snapshot = WatchlistSnapshot(
        session_date=day,
        asof=asof,
        strategy_version=STRATEGY_VERSION_V2,
        selection_version=SELECTION_VERSION_V2,
        versions=watchlist_service.versions_used(overlays),
        inputs=_inputs(session, pool, members, picked, swept, asof=asof, morning=morning),
        pool=len(ids),
        left_out=chosen.left_out,
        pool_id=pool.id,
    )
    session.add(snapshot)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    regimes: dict[str, str] = {}
    for pick in chosen.picks:
        i = pick.instrument_id
        m = by_id[i]
        inst = session.get(Instrument, i)
        assert inst is not None
        code = regime_service.index_for(inst.market, inst.listing)
        if code not in regimes:
            regimes[code] = regime_service.regime_at(session, code, asof).label
        over = overlays.get(i)
        session.add(
            WatchlistMember(
                snapshot_id=snapshot.id,
                instrument_id=i,
                rank=pick.rank,
                reasons=list(pick.reasons),
                tracked=m.tracked,
                overlay_points=over.overlay.points if over else None,
                overlay_events=overlay_service.detail(over, limit=5) if over else [],
                attention_status=attention[i][0],
                attention_surge=attention[i][1],
                discovery_score=m.discovery_score,
                regime=regimes[code],
                signal_decision_at=None,
                total_score=m.total_score,
                technical_score=m.technical_score,
                fundamental_score=m.fundamental_score,
                last_action=m.action,
                news_swept_at=swept.get(i),
                score_source="PREOPEN",
                evaluated_at=m.evaluated_at,
                price_data_asof=m.price_data_asof,
                fundamental_data_asof=m.fundamental_data_asof,
                fundamental_checked_at=m.fundamental_checked_at,
                peer_count=m.peer_count,
                peer_hash=m.peer_hash,
                prefetch_status=m.prefetch_status,
                abstained_reason=m.abstained_reason,
            )
        )
    session.commit()
    _mark(
        session,
        pool,
        SNAPSHOT,
        SUCCESS,
        detail=f"{len(chosen.picks)} of {len(ids)} names, {chosen.left_out} left out",
    )
    logger.info(
        "watchlist v2 %s: %d of %d names, %d left out (pool %s)",
        day,
        len(chosen.picks),
        len(ids),
        chosen.left_out,
        pool.status,
    )
    return snapshot


# --- 참고용 재계산 -------------------------------------------------------------------


def dry_run(session: Session, *, asof: datetime) -> dict[str, object]:
    """과거 아침을 그때까지 저장된 데이터로만 V2로 다시 골라 본다. 아무것도 쓰지 않는다.

    참고용이다. 그때는 07:00 사전 수집도 07:00 LLM도 08:30 보충도 없었으므로,
    "그날 V2를 돌렸다면 이 목록"이 아니다. 분포(날마다 몇 개, 어떤 이유)를 보는
    용도로만 쓴다. 발굴·공시·관심 종목·뉴스 점수·검색 추세 모두 `asof`에
    기록돼 있던 것만 읽는다.
    """
    asof = ensure_utc(asof, field="asof")
    day = KR.local_today(asof)
    korean = {
        i.instrument_id: i
        for i in instrument_repo.list_active(session, asof=day, market=Market.KR, tracked=None)
    }
    later = promotion_repo.promoted_after(session, asof)
    tracked = {i for i, inst in korean.items() if inst.tracked and i not in later}
    found = discovery_service.discover(session, asof=asof, top=DISCOVERY_TOP)
    discovery = {c.instrument_id: c.score for c in found.candidates}
    filed = disclosure_repo.filed_between(
        session,
        first=_previous_session(day),
        before=day,
        stored_by=asof,
        instrument_ids=list(korean),
    )
    intensity = event_intensity(filed)
    with_event = set(intensity)
    focus = set(llm_service.focus_ids_asof(session, asof))
    ids = sorted((set(discovery) | with_event | focus) & set(korean))
    overlays = overlay_service.overlays_at(session, asof=asof, instrument_ids=ids)
    seen = []
    for i in ids:
        att, _ = attention_service.attention_at(session, i, asof)
        seen.append(
            Seen(
                instrument_id=i,
                tracked=i in tracked,
                overlay_points=overlays[i].overlay.points if i in overlays else None,
                has_disclosure_event=i in with_event,
                disclosure_intensity=intensity.get(i),
                search_surge=att.surge,
                discovery_score=discovery.get(i),
            )
        )
    chosen = select_names_v2(seen)
    reasons: Counter[str] = Counter(r for p in chosen.picks for r in p.reasons)
    picked = {p.instrument_id for p in chosen.picks}
    previous_close = KR.session_close(_previous_session(day))
    would_fetch = sum(
        1 for i in picked if (at := _latest_bar_at(session, i, asof)) is None or at < previous_close
    )
    return {
        "day": day.isoformat(),
        "asof": asof.isoformat(),
        "pool": len(ids),
        "listed": len(chosen.picks),
        "left_out": chosen.left_out,
        "tracked_listed": len(picked & tracked),
        "reasons": dict(reasons),
        "names": [(p.rank, korean[p.instrument_id].name, list(p.reasons)) for p in chosen.picks],
        "stale_prices_among_listed": would_fetch,
        "news_freshness": found.freshness.value,
    }
