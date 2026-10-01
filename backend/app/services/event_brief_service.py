"""목록 종목의 뉴스·공시마다 쉬운 설명을 붙인다: 무슨 일, 회사에 좋은 일인지(이유), 이런 일이 생기면 보통(기록). 읽기만 한다.

판단 보조다(사라/팔라를 말하지 않는다). 입력은 관찰 목록 행의 사건(오버레이 묶음)이고, 출력은 같은 사건에 설명 칸을 더한 것과,
목록 이유가 된 공시 중 사건 칸에 없는 것(공시는 다음 개장부터 쓸 수 있어 아침 오버레이에는 하루 늦은 공시만 있다)을 앞에 더한 것이다.

- 뉴스 "무슨 일": 오버레이와 같은 모델·프롬프트로 그 종목에 대해 읽은 `evidence`(기사 속 근거 문장). 없으면 제목.
- 공시 "무슨 일"·좋음/나쁨: 제목 규칙(`disclosure_events.matched`)의 쉬운 말.
- "보통": 공시는 3개월 기준표(+ 우리 목록 기록), 뉴스는 우리 목록 기록만(과거 뉴스 이력이 없다).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import ensure_utc
from app.models.disclosure import Disclosure
from app.models.news import NewsSentiment
from app.models.watchlist import WatchlistMember, WatchlistSnapshot
from app.repositories import disclosure_repo
from app.scoring import disclosure_events, event_explain
from app.scoring.watchlist import STRATEGY_VERSION_V2
from app.services import reaction_service
from app.services.llm_service import SENTIMENT_PROMPT_VERSION

logger = logging.getLogger(__name__)
KR = MarketCalendar(Market.KR)
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="
DISCLOSURE_REASON = "DISCLOSURE_EVENT"


def previous_session(day: date) -> date | None:
    before = KR.sessions_between(day - timedelta(days=14), day - timedelta(days=1))
    return before[-1] if before else None


def disclosure_events_for(
    session: Session, day: date, asof: datetime, ids: Sequence[int]
) -> dict[int, list[dict[str, Any]]]:
    """목록 이유가 된 사건 공시(전 거래일~전날 접수, `asof`까지 저장). 최근 접수 먼저."""
    prev = previous_session(day)
    if not ids or prev is None:
        return {}
    filed = disclosure_repo.filed_between(
        session, first=prev, before=day, stored_by=asof, instrument_ids=list(ids)
    )
    events = [d for d in filed if disclosure_events.classify(d.report_nm) is not None]
    if not events:
        return {}
    receipts: dict[int, str] = {
        row[0]: row[1]
        for row in session.execute(
            select(Disclosure.id, Disclosure.rcept_no).where(
                Disclosure.id.in_([d.id for d in events])
            )
        ).all()
    }
    out: dict[int, list[dict[str, Any]]] = {}
    for d in sorted(events, key=lambda x: receipts.get(x.id, ""), reverse=True):
        found = disclosure_events.classify(d.report_nm)
        out.setdefault(d.instrument_id, []).append(
            {
                "event_type": found.event_type if found else "OTHER",
                "first_at": "",
                "title": f"[공시] {d.report_nm}",
                "sentiment": float(found.sentiment or 0.0) if found else 0.0,
                "articles": 1,
                "disclosures": 1,
                "url": f"{DART_VIEW}{receipts[d.id]}" if receipts.get(d.id) else None,
                "lead_source": "DART",
                "lead_id": d.id,
                "report_nm": d.report_nm,
            }
        )
    return out


# --- 우리 목록 기록 -----------------------------------------------------------------------------------------

_OURS: dict[date, dict[str, reaction_service.Stat]] = {}


def our_stats(session: Session, day: date) -> dict[str, reaction_service.Stat]:
    """`day` 전 목록 날들의 기록을 키별로(공시 세부 키, "NEWS|종류|방향"). 날짜별로 한 번 계산해 둔다."""
    if day in _OURS:
        return _OURS[day]
    paths = reaction_service.list_paths(session, before=day)
    keyed: list[tuple[str, Any]] = []
    snaps = session.execute(
        select(WatchlistSnapshot.session_date, WatchlistSnapshot.asof, WatchlistMember)
        .join(WatchlistMember, WatchlistMember.snapshot_id == WatchlistSnapshot.id)
        .where(
            WatchlistSnapshot.session_date < day,
            WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
        )
    ).all()
    by_day: dict[date, list[tuple[datetime, WatchlistMember]]] = {}
    for d, asof, m in snaps:
        by_day.setdefault(d, []).append((asof, m))
    for d, members in by_day.items():
        asof = members[0][0]
        disc = disclosure_events_for(session, d, asof, [m.instrument_id for _, m in members])
        for _, m in members:
            path = paths.get((d, m.instrument_id))
            if path is None:
                continue
            for e in disc.get(m.instrument_id, []):
                key = reaction_service.disclosure_key(e["report_nm"])
                if key:
                    keyed.append((key[0], path))
            for e in m.overlay_events or []:
                if isinstance(e, dict) and (e.get("lead") or {}).get("source") == "NEWS":
                    verdict = event_explain.news_verdict(float(e.get("sentiment") or 0.0))
                    keyed.append(
                        (f"NEWS|{e.get('event_type')}|{event_explain.direction(verdict)}", path)
                    )
                    break  # 종목당 대표 뉴스 묶음 하나
    _OURS[day] = reaction_service.group_paths(keyed)
    return _OURS[day]


# --- 설명 붙이기 ------------------------------------------------------------------------------------------------


def _evidence(
    session: Session, pairs: set[tuple[int, int]], asof: datetime
) -> dict[tuple[int, int], str]:
    if not pairs:
        return {}
    settings = get_settings()
    rows = session.execute(
        select(
            NewsSentiment.news_item_id,
            NewsSentiment.instrument_id,
            NewsSentiment.evidence,
            NewsSentiment.created_at,
        ).where(
            NewsSentiment.news_item_id.in_([p[0] for p in pairs]),
            NewsSentiment.model == settings.sentiment_llm_model,
            NewsSentiment.prompt_version == SENTIMENT_PROMPT_VERSION,
            NewsSentiment.created_at <= ensure_utc(asof, field="asof"),
        )
    ).all()
    return {(n, i): str(ev) for n, i, ev, _ in rows if (n, i) in pairs and ev}


def explain(
    session: Session,
    day: date,
    asof: datetime,
    members: Sequence[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """종목마다 설명을 붙인 사건 목록. members: {"instrument_id", "reasons", "events": [사건 dict]}."""
    ids = [int(m["instrument_id"]) for m in members]
    need_disc = [
        int(m["instrument_id"]) for m in members if DISCLOSURE_REASON in (m.get("reasons") or [])
    ]
    # 오버레이의 공시 사건은 하루 늦은 옛 공시일 수 있다(공시는 다음 개장부터 쓴다). 목록 이유가 된 공시 중
    # 사건 칸에 없는 것을 앞에 더한다(카톡은 종목당 사건 둘까지라 이유 공시가 먼저 보이게).
    try:
        with session.begin_nested():  # SQL 오류가 바깥 트랜잭션을 깨지 않게
            found = disclosure_events_for(session, day, asof, need_disc)
    except Exception:  # 설명은 보조다: 덧붙이기가 실패해도 나머지 설명은 붙인다
        logger.exception("event brief: reason disclosures failed")
        found = {}
    shown = {
        int(e["lead_id"])
        for m in members
        for e in m.get("events") or []
        if e.get("lead_source") == "DART" and e.get("lead_id")
    }
    added = {i: [e for e in es if e["lead_id"] not in shown] for i, es in found.items()}
    dart_ids = sorted(shown)
    titles: dict[int, str] = (
        {
            row[0]: row[1]
            for row in session.execute(
                select(Disclosure.id, Disclosure.report_nm).where(Disclosure.id.in_(dart_ids))
            ).all()
        }
        if dart_ids
        else {}
    )
    pairs = {
        (int(e["lead_id"]), int(m["instrument_id"]))
        for m in members
        for e in m.get("events") or []
        if e.get("lead_source") == "NEWS" and e.get("lead_id")
    }
    evidence = _evidence(session, pairs, asof)
    try:
        with session.begin_nested():
            ours = our_stats(session, day)
    except Exception:  # 우리 목록 기록이 없어도 쉬운 말과 3개월 기준표는 붙인다
        logger.exception("event brief: list records failed")
        ours = {}
    out: dict[int, list[dict[str, Any]]] = {}
    for m in members:
        iid = int(m["instrument_id"])
        events = added.get(iid, []) + [dict(e) for e in m.get("events") or []]
        for e in events:
            e.update(_explained(e, iid, titles, evidence, ours))
        out[iid] = events
    return {i: out[i] for i in ids}


def _explained(
    e: dict[str, Any],
    iid: int,
    titles: dict[int, str],
    evidence: dict[tuple[int, int], str],
    ours: dict[str, reaction_service.Stat],
) -> dict[str, Any]:
    title = str(e.get("title") or "")
    report = e.get("report_nm") or (
        titles.get(int(e["lead_id"]))
        if e.get("lead_source") == "DART" and e.get("lead_id")
        else None
    )
    if report is None and title.startswith("[공시]"):
        report = title.removeprefix("[공시]").strip()  # lead가 없는 옛 행(9/28)
    if report:
        found = disclosure_events.matched(report)
        if found is not None:
            phrase, subsidiary, _ = found
            plain = event_explain.disclosure(phrase, subsidiary)
            stat, scope = reaction_service.disclosure_stat(report)
            key = reaction_service.disclosure_key(report)
            mine = ours.get(key[0]) if key else None
            return _fields(plain, stat, scope, mine)
    what = evidence.get((int(e["lead_id"]), iid)) if e.get("lead_id") else None
    plain = event_explain.news(
        str(e.get("event_type") or "OTHER"), float(e.get("sentiment") or 0.0), what or title
    )
    mine = ours.get(f"NEWS|{e.get('event_type')}|{event_explain.direction(plain.verdict)}")
    return _fields(plain, None, "", mine)


def _fields(
    plain: event_explain.Plain,
    stat: reaction_service.Stat | None,
    scope: str,
    mine: reaction_service.Stat | None,
) -> dict[str, Any]:
    usual = reaction_service.fmt(stat, f"지난 3개월 {scope}") if stat else None
    usual_ours = (
        reaction_service.fmt(mine, "우리 목록")
        if mine is not None and mine.n >= reaction_service.MIN_N
        else None
    )
    return {
        "kind": plain.kind,
        "what": plain.what,
        "verdict": plain.verdict,
        "why": plain.why,
        "verdict_source": plain.source,
        "usual": usual,
        "usual_short": reaction_service.fmt(stat, f"3개월 {scope}", short=True) if stat else None,
        "usual_ours": usual_ours,
        "usual_ours_short": reaction_service.fmt(mine, "우리 목록", short=True)
        if usual_ours is not None
        else None,
    }
