"""장 시작 직후 동반주: 관계 찾기(이름 묶음 + AI), 밤에 AI에게 관련주 묻기, 화면용 조회, 1분봉 대상. 표시·기록 전용.

규칙과 계산은 `app/scoring/companions.py`(순수), 실시간 수집은 `app/collectors/kis_open_movers.py`가 한다.

**AI 묻기(`ask_ai`).** 대상은 최근 10거래일 장 초반 순위(`open_mover`)에 든 종목과 가장 최근 아침 목록, 그리고 사람이 넘긴 이름.
30일 안에 이미 물은 종목은 빼고 25개씩 묻는다(최대 `max_batches`호출). 다른 LLM 실행과 같은 잠금(`llm_run`)을 잡고, 시작 전과 매 호출
뒤 구독 사용률을 보고(`llm_max_five_hour_utilization`·`llm_max_seven_day_utilization`), 호출마다 `claude_subscription` 한도를 예약하고,
모든 호출을 `llm_call`에 남긴다(purpose "companions"). 원 응답은 `companion_ask`에 그대로 남긴다(고치지 않는다).
원형(2026-10-12)에서 종목코드는 대부분 틀려서 이름만 받는다. 이름은 정규화해 마스터와 정확히 하나가 맞을 때만 관계로 남기고, 같은
질문 묶음에 있던 종목을 고른 관계는 `in_batch`로 표시한다(화면에는 이름 규칙도 같은 관계를 낼 때만 — `scoring.companions.merge`).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Collection, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.collectors.quota import QuotaExhausted, QuotaGuard
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.db import advisory_lock, session_scope
from app.llm.provider import LlmProvider, LlmRateLimitedError, LlmUnavailableError
from app.models import (
    CollectorRun,
    CompanionAlert,
    CompanionAsk,
    CompanionRelation,
    Instrument,
    OpenMover,
    WatchlistMember,
    WatchlistSnapshot,
)
from app.repositories import instrument_repo, llm_repo
from app.scoring import companions as cp
from app.scoring.watchlist import STRATEGY_VERSION_V2
from app.services import llm_service

logger = logging.getLogger(__name__)
KR = MarketCalendar(Market.KR)

PURPOSE = "companions"
PROMPT_VERSION = 1
BATCH = 25
ASK_AGAIN = timedelta(days=30)
RECENT_SESSIONS = 10
MINUTE_TARGETS = 30

SYSTEM = (
    "너는 한국 주식시장 단타 트레이더를 돕는 분석가다. 각 종목에 대해, 그 종목이 급등할 때 한국 증시에서 흔히 함께 움직이는 "
    "'관련주'(같은 그룹·계열, 같은 이름, 같은 지역 연고, 같은 테마·사업)를 KOSPI·KOSDAQ 상장사 중 최대 5개 적어라. 종목명은 KRX 정식 "
    "종목명(예: 현대차, SK텔레콤, 한국항공우주)으로, 이유는 30자 이내. 목록의 종목들은 서로 관계없이 섞어 놓은 것이니 목록 안에서 "
    "관련주를 고르지 마라(정말 같은 그룹·같은 이름일 때만 예외). 모르면 비워 둬라(지어내지 마라)."
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "related": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["name", "reason"],
                        },
                    },
                },
                "required": ["id", "related"],
            },
        }
    },
    "required": ["items"],
}

_NAMES: dict[date, dict[int, str]] = {}


def _master(session: Session, day: date) -> dict[int, str]:
    """KR 활성 종목 id → 이름. 하루에 한 번만 읽는다(매분 잡이 2,648개를 다시 읽지 않게)."""
    cached = _NAMES.get(day)
    if cached is None:
        cached = {
            i.instrument_id: i.name
            for i in instrument_repo.list_active(session, asof=day, market=Market.KR, tracked=None)
        }
        _NAMES.clear()
        _NAMES[day] = cached
    return cached


def companions_for(session: Session, leader_ids: Sequence[int]) -> dict[int, list[cp.Related]]:
    """이끄는 종목 → 관계(이름 묶음 + 가장 최근에 물은 AI 관계)."""
    day = KR.local_today(utc_now())
    by_name = cp.name_groups(_master(session, day))
    by_ai: dict[int, list[tuple[int, str, bool]]] = defaultdict(list)
    if leader_ids:
        latest = (
            select(CompanionRelation.instrument_id, func.max(CompanionRelation.ask_id).label("ask"))
            .where(CompanionRelation.instrument_id.in_(leader_ids))
            .group_by(CompanionRelation.instrument_id)
            .subquery()
        )
        for i, rid, reason, in_batch in session.execute(
            select(
                CompanionRelation.instrument_id,
                CompanionRelation.related_id,
                CompanionRelation.reason,
                CompanionRelation.in_batch,
            ).join(
                latest,
                (latest.c.instrument_id == CompanionRelation.instrument_id)
                & (latest.c.ask == CompanionRelation.ask_id),
            )
        ).all():
            by_ai[int(i)].append((int(rid), str(reason or ""), bool(in_batch)))
    return {i: cp.merge(i, by_name, by_ai) for i in leader_ids}


# --- 밤에 AI에게 묻기 ---------------------------------------------------------------------------------------------


def targets(session: Session, *, now: datetime, extra: Collection[int] = ()) -> list[int]:
    """물을 종목: 최근 10거래일 장 초반 순위, 가장 최근 아침 목록, extra를 합쳐, 30일 안에 물은 것 빼고."""
    day = KR.local_today(now)
    sessions = KR.sessions_between(day - timedelta(days=30), day)[-RECENT_SESSIONS:]
    ids: set[int] = set(extra)
    if sessions:
        ids |= set(
            session.scalars(
                select(OpenMover.instrument_id)
                .where(OpenMover.session_date >= sessions[0])
                .distinct()
            )
        )
    snap = session.scalar(
        select(WatchlistSnapshot.id)
        .where(WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2)
        .order_by(WatchlistSnapshot.session_date.desc())
        .limit(1)
    )
    if snap is not None:
        ids |= set(
            session.scalars(
                select(WatchlistMember.instrument_id).where(WatchlistMember.snapshot_id == snap)
            )
        )
    asked: set[int] = set()
    for batch in session.scalars(
        select(CompanionAsk.batch).where(
            CompanionAsk.asked_at >= now - ASK_AGAIN, CompanionAsk.status == "OK"
        )
    ):
        asked |= {int(b["instrument_id"]) for b in batch if isinstance(b, dict)}
    return sorted(ids - asked)


def ask_ai(
    session: Session,
    *,
    extra: Collection[int] = (),
    max_batches: int = 6,
    provider: LlmProvider | None = None,
    guard: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """밤 잡·CLI: 관련주를 묻는다. 다른 LLM 실행이 돌고 있으면 기다리지 않고 멈춘다."""
    with (
        session_scope() as lock_session,
        advisory_lock(lock_session, llm_service.LLM_RUN_LOCK) as held,
    ):
        if not held:
            return {"asked": 0, "calls": 0, "stopped": "another LLM run is in progress"}
        return _ask(
            session,
            extra=extra,
            max_batches=max_batches,
            provider=provider or llm_service.default_provider(),
            guard=guard or QuotaGuard(),
            now=now or utc_now(),
        )


def _ask(
    session: Session,
    *,
    extra: Collection[int],
    max_batches: int,
    provider: LlmProvider,
    guard: Any,
    now: datetime,
) -> dict[str, Any]:
    settings = get_settings()
    model = settings.sentiment_llm_model
    day = KR.local_today(now)
    names = _master(session, day)
    by_norm: dict[str, list[int]] = defaultdict(list)
    for i, n in names.items():
        by_norm[cp.normalize(n)].append(i)
    todo = [i for i in targets(session, now=now, extra=extra) if i in names]
    report: dict[str, Any] = {"targets": len(todo), "asked": 0, "calls": 0, "relations": 0}
    report["unresolved"] = 0
    five, seven = llm_repo.recent_utilization(session, provider=provider.name)
    if todo and five is not None and five >= settings.llm_max_five_hour_utilization:
        report["stopped"] = f"five-hour usage at {five:.0%} by the last call; not starting"
        return report
    if todo and seven is not None and seven >= settings.llm_max_seven_day_utilization:
        report["stopped"] = f"seven-day usage at {seven:.0%} by the last call; not starting"
        return report
    for start in range(0, min(len(todo), BATCH * max_batches), BATCH):
        batch = todo[start : start + BATCH]
        members = [{"n": k, "instrument_id": i, "name": names[i]} for k, i in enumerate(batch, 1)]
        try:
            guard.reserve(llm_service.QUOTA_GROUP, PURPOSE)
        except QuotaExhausted as refused:
            report["stopped"] = f"call quota: {refused}"
            break
        report["calls"] += 1
        prompt = "종목 목록(번호. 종목명):\n" + "\n".join(f"{m['n']}. {m['name']}" for m in members)
        try:
            result = provider.complete(system=SYSTEM, prompt=prompt, schema=SCHEMA, model=model)
        except (LlmRateLimitedError, LlmUnavailableError) as exc:
            status = "RATE_LIMITED" if isinstance(exc, LlmRateLimitedError) else "UNAVAILABLE"
            _save_ask(session, now, model, members, None, status)
            llm_service._ledger(
                session,
                provider,
                PURPOSE,
                model,
                PROMPT_VERSION,
                len(batch),
                status,
                error=str(exc),
            )
            report["stopped"] = f"{status}: {exc}"
            break
        except Exception as exc:
            _save_ask(session, now, model, members, None, "ERROR")
            llm_service._ledger(
                session,
                provider,
                PURPOSE,
                model,
                PROMPT_VERSION,
                len(batch),
                "ERROR",
                error=repr(exc),
            )
            raise
        answer = result.structured if isinstance(result.structured, dict) else None
        items = answer.get("items") if answer else None
        if not isinstance(items, list):
            _save_ask(session, now, model, members, answer, "MALFORMED")
            llm_service._ledger(
                session, provider, PURPOSE, model, PROMPT_VERSION, len(batch), "MALFORMED", result
            )
            continue
        ask_id = _save_ask(session, now, model, members, answer, "OK")
        in_batch_ids = set(batch)
        rows: list[dict[str, Any]] = []
        for item in items:
            num = item.get("id") if isinstance(item, dict) else None
            if not isinstance(num, int) or not 1 <= num <= len(batch):
                continue
            me = batch[num - 1]
            related = [r for r in item.get("related") or [] if isinstance(r, dict)]
            resolved = cp.resolve_names([str(r.get("name") or "") for r in related], by_norm)
            for r, rid in zip(related, resolved, strict=True):
                if rid is None:
                    report["unresolved"] += 1
                    continue
                if rid == me:
                    continue
                rows.append(
                    {
                        "ask_id": ask_id,
                        "instrument_id": me,
                        "related_id": rid,
                        "reason": str(r.get("reason") or "")[:200] or None,
                        "in_batch": rid in in_batch_ids,
                    }
                )
        if rows:
            session.execute(pg_insert(CompanionRelation).values(rows).on_conflict_do_nothing())
        report["asked"] += len(batch)
        report["relations"] += len(rows)
        llm_service._ledger(
            session, provider, PURPOSE, model, PROMPT_VERSION, len(batch), "OK", result
        )
        five, seven = result.usage.five_hour_utilization, result.usage.seven_day_utilization
        if five is not None and five >= settings.llm_max_five_hour_utilization:
            report["stopped"] = f"five-hour usage at {five:.0%}"
            break
        if seven is not None and seven >= settings.llm_max_seven_day_utilization:
            report["stopped"] = f"seven-day usage at {seven:.0%}"
            break
    return report


def _save_ask(
    session: Session,
    now: datetime,
    model: str,
    members: list[dict[str, Any]],
    answer: dict[str, Any] | None,
    status: str,
) -> int:
    ask = CompanionAsk(
        asked_at=now,
        model=model,
        prompt_version=PROMPT_VERSION,
        batch=members,
        answer=answer,
        status=status,
    )
    session.add(ask)
    session.flush()
    return int(ask.id)


# --- 화면·1분봉 -----------------------------------------------------------------------------------------------------


def latest_day(session: Session) -> date | None:
    return session.scalar(select(func.max(CompanionAlert.session_date)))


def today_view(session: Session, day: date | None = None) -> dict[str, Any]:
    """그날 마지막 수집 시각의 이끄는 종목과 동반 후보. 마지막 수집 실행의 상태도 함께(건너뛴 분이 있으면 보이게)."""
    target = day or latest_day(session)
    run = session.execute(
        select(CollectorRun.status, CollectorRun.started_at, CollectorRun.detail)
        .where(CollectorRun.source == "KIS_OPEN_MOVERS")
        .order_by(CollectorRun.started_at.desc())
        .limit(1)
    ).first()
    last_run = (
        None
        if run is None
        else {"status": str(run[0].value), "at": run[1].isoformat(), "detail": run[2]}
    )
    if target is None:
        return {"day": None, "at": None, "leaders": [], "last_run": last_run}
    at = session.scalar(
        select(func.max(CompanionAlert.at)).where(CompanionAlert.session_date == target)
    )
    rows = list(
        session.execute(
            select(CompanionAlert).where(
                CompanionAlert.session_date == target, CompanionAlert.at == at
            )
        ).scalars()
    )
    ids = {r.leader_id for r in rows} | {r.companion_id for r in rows}
    names = dict(
        session.execute(
            select(Instrument.instrument_id, Instrument.name).where(
                Instrument.instrument_id.in_(ids)
            )
        )
        .tuples()
        .all()
    )
    by_leader: dict[int, list[CompanionAlert]] = defaultdict(list)
    for r in rows:
        by_leader[r.leader_id].append(r)
    leaders = sorted(by_leader.items(), key=lambda kv: -kv[1][0].leader_change)
    return {
        "day": target.isoformat(),
        "at": at.isoformat() if at else None,
        "last_run": last_run,
        "leaders": [
            {
                "instrument_id": lid,
                "name": names.get(lid, "?"),
                "change_pct": group[0].leader_change,
                "companions": [
                    {
                        "instrument_id": r.companion_id,
                        "name": names.get(r.companion_id, "?"),
                        "change_pct": r.companion_change,
                        "price": r.companion_price,
                        "fetched_at": r.companion_fetched_at.isoformat()
                        if r.companion_fetched_at
                        else None,
                        "sources": r.sources,
                        "reason": r.reason,
                    }
                    for r in sorted(
                        group,
                        key=lambda r: (r.companion_change is None, r.companion_change or 0.0),
                    )
                ],
            }
            for lid, group in leaders
        ],
    }


def minute_targets_today(session: Session) -> list[int]:
    """오늘 알림의 이끄는 종목·동반 후보(이끄는 종목 등락률 순, 최대 30). 1분봉 수집 대상에 더한다."""
    day = KR.local_today(utc_now())
    rows = session.execute(
        select(CompanionAlert.leader_id, CompanionAlert.companion_id, CompanionAlert.leader_change)
        .where(CompanionAlert.session_date == day)
        .order_by(CompanionAlert.leader_change.desc())
    ).all()
    out: list[int] = []
    for leader, companion, _ in rows:
        for i in (int(leader), int(companion)):
            if i not in out:
                out.append(i)
    return out[:MINUTE_TARGETS]
