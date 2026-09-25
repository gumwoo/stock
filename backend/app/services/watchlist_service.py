"""The morning watchlist: read everything as of one moment before the open, choose, freeze.

The pool is the names in focus (tracked Korean names and recent candidates),
the news-surge candidates at the moment, and any name with an event
disclosure filed since the last session. Each is read as of the snapshot's
moment — overlay, search attention, discovery score, regime, the last close's
signal — and the rule in `app/scoring/watchlist.py` chooses and ranks. What
was read is stored with each member, and what the morning's inputs were in is
stored with the snapshot.

One snapshot a trading day under a strategy version: the first is the record,
and a second run that day does nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collectors.naver_news import RULE_VERSION as RELEVANCE_RULE_VERSION
from app.core.calendar import Market, MarketCalendar
from app.core.clock import ensure_utc, utc_now
from app.core.types import Engine
from app.models import CollectorRun, Instrument, Signal, SignalFactor
from app.models.llm import LlmCall
from app.models.news import NewsSource, NewsSweepCoverage
from app.models.watchlist import WatchlistMember, WatchlistSnapshot
from app.repositories import disclosure_repo, instrument_repo, promotion_repo
from app.scoring import disclosure_events
from app.scoring.policy import STRATEGY_VERSION as SIGNAL_STRATEGY
from app.scoring.watchlist import SELECTION_VERSION, STRATEGY_VERSION, Seen, select_names
from app.services import (
    attention_service,
    discovery_service,
    llm_service,
    overlay_service,
    regime_service,
)
from app.services.llm_service import RELEVANCE_PROMPT_VERSION, SENTIMENT_PROMPT_VERSION

logger = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")
# The morning's inputs are "in" if they ran after this, the day's first job.
MORNING = time(7, 30)


def _latest_signals(
    session: Session, ids: list[int], asof: datetime
) -> dict[int, tuple[Signal, dict[Engine, float]]]:
    """The newest signal of each name made by `asof` under the scoring strategy, with its engines."""
    newest = (
        select(Signal.instrument_id, func.max(Signal.decision_at).label("at"))
        .where(
            Signal.instrument_id.in_(ids),
            Signal.strategy_version == SIGNAL_STRATEGY,
            Signal.decision_at <= asof,
            Signal.ingested_at <= asof,
        )
        .group_by(Signal.instrument_id)
        .subquery()
    )
    rows = session.execute(
        select(Signal)
        .join(
            newest,
            (Signal.instrument_id == newest.c.instrument_id) & (Signal.decision_at == newest.c.at),
        )
        .where(Signal.strategy_version == SIGNAL_STRATEGY, Signal.ingested_at <= asof)
        .order_by(Signal.id)
    ).scalars()
    out: dict[int, tuple[Signal, dict[Engine, float]]] = {}
    for signal in rows:
        if signal.instrument_id in out:
            continue  # the first made for that close, as the forward record counts it
        factors = session.execute(
            select(SignalFactor.engine, SignalFactor.score).where(
                SignalFactor.signal_id == signal.id
            )
        ).all()
        out[signal.instrument_id] = (signal, {e: s for e, s in factors})  # noqa: C416 - rows, not pairs
    return out


def _swept(session: Session, ids: list[int], asof: datetime) -> dict[int, datetime]:
    c = NewsSweepCoverage
    rows = session.execute(
        select(c.instrument_id, func.max(c.covered_to))
        .where(
            c.instrument_id.in_(ids),
            c.source == NewsSource.NAVER_NEWS,
            c.collector == "NAVER_NEWS",
            c.recorded_at <= asof,
        )
        .group_by(c.instrument_id)
    ).all()
    return {i: t for i, t in rows}  # noqa: C416 - rows, not pairs


def _inputs(
    session: Session, asof: datetime, morning: datetime, swept: dict[int, datetime], ids: list[int]
) -> dict[str, object]:
    """What the morning's inputs were in, as of the moment."""

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
            "run": last_run("NAVER_NEWS"),
            "members": len(ids),
            "swept_this_morning": sum(1 for i in ids if swept.get(i, morning) > morning),
        },
        "llm": {"calls": sum(by_status.values()), "by_status": by_status},
        "search_trends": last_run("NAVER_DATALAB"),
        "disclosures": last_run("DART_DISCLOSURE"),
    }


def take_snapshot(
    session: Session,
    *,
    now: datetime | None = None,
    only: Collection[int] | None = None,
) -> WatchlistSnapshot | None:
    """Freeze this morning's list. None on a day without a session, or if one is already taken.

    `only` narrows the pool to the named instruments; production never passes it.
    """
    asof = ensure_utc(now, field="now") if now is not None else utc_now()
    calendar = MarketCalendar(Market.KR)
    day = calendar.local_today(asof)
    if not calendar.is_session(day):
        return None
    if asof >= calendar.session_open(day):
        # A list taken after the open would be chosen on the session it is
        # to be judged on. A morning that missed its moment has no list.
        logger.warning("watchlist: %s is past the open; no list for %s", asof, day)
        return None
    already = session.execute(
        select(WatchlistSnapshot.id).where(
            WatchlistSnapshot.session_date == day,
            WatchlistSnapshot.strategy_version == STRATEGY_VERSION,
        )
    ).first()
    if already is not None:
        logger.info("watchlist: %s already has a snapshot", day)
        return None
    morning = datetime.combine(day, MORNING, tzinfo=SEOUL)

    # Tracked as of the moment: a name promoted later was not tracked then.
    later = promotion_repo.promoted_after(session, asof)
    korean = {
        i.instrument_id: i
        for i in instrument_repo.list_active(session, asof=day, market=Market.KR, tracked=None)
    }
    tracked = {i for i, inst in korean.items() if inst.tracked and i not in later}

    found = discovery_service.discover(session, asof=asof)
    discovery = {c.instrument_id: c.score for c in found.candidates}

    previous = calendar.sessions_between(day - timedelta(days=14), day - timedelta(days=1))[-1]
    filed = disclosure_repo.filed_between(
        session, first=previous, before=day, stored_by=asof, instrument_ids=list(korean)
    )
    with_event = {d.instrument_id for d in filed if disclosure_events.classify(d.report_nm)}

    pool_ids = sorted(
        (set(llm_service.focus_ids(session)) | set(discovery) | with_event | tracked) & set(korean)
    )
    if only is not None:
        pool_ids = [i for i in pool_ids if i in set(only)]
    overlays = overlay_service.overlays_at(session, asof=asof, instrument_ids=pool_ids)
    signals = _latest_signals(session, pool_ids, asof)
    swept = _swept(session, pool_ids, asof)

    seen: dict[int, Seen] = {}
    attention: dict[int, tuple[str, float | None]] = {}
    for i in pool_ids:
        found_attention, _ = attention_service.attention_at(session, i, asof)
        attention[i] = (found_attention.status, found_attention.surge)
        signal = signals.get(i)
        seen[i] = Seen(
            instrument_id=i,
            tracked=i in tracked,
            overlay_points=overlays[i].overlay.points if i in overlays else None,
            has_disclosure_event=i in with_event,
            search_surge=found_attention.surge,
            discovery_score=discovery.get(i),
            last_action=signal[0].action.value if signal is not None else None,
        )
    chosen = select_names(list(seen.values()))

    snapshot = WatchlistSnapshot(
        session_date=day,
        asof=asof,
        strategy_version=STRATEGY_VERSION,
        selection_version=SELECTION_VERSION,
        versions={
            "overlay": overlay_service.PARAMS.version,
            "reading_model": next(iter(overlays.values())).model if overlays else None,
            "relevance_rule": RELEVANCE_RULE_VERSION,
            "relevance_prompt": RELEVANCE_PROMPT_VERSION,
            "sentiment_prompt": SENTIMENT_PROMPT_VERSION,
            "attention": attention_service.PARAMS.version,
            "regime": regime_service.PARAMS.version,
            "disclosure_rule": disclosure_events.RULE_VERSION,
            "signal_strategy": SIGNAL_STRATEGY,
        },
        inputs=_inputs(session, asof, morning, swept, [p.instrument_id for p in chosen.picks]),
        pool=len(pool_ids),
        left_out=chosen.left_out,
    )
    session.add(snapshot)
    try:
        session.flush()
    except IntegrityError:
        # Another run froze this morning between our check and our write.
        session.rollback()
        return None
    regimes: dict[str, str] = {}
    for pick in chosen.picks:
        i = pick.instrument_id
        inst: Instrument = korean[i]
        code = regime_service.index_for(inst.market, inst.listing)
        if code not in regimes:
            regimes[code] = regime_service.regime_at(session, code, asof).label
        signal = signals.get(i)
        over = overlays.get(i)
        session.add(
            WatchlistMember(
                snapshot_id=snapshot.id,
                instrument_id=i,
                rank=pick.rank,
                reasons=list(pick.reasons),
                tracked=i in tracked,
                overlay_points=over.overlay.points if over else None,
                overlay_events=overlay_service.detail(over, limit=5) if over else [],
                attention_status=attention[i][0],
                attention_surge=attention[i][1],
                discovery_score=discovery.get(i),
                regime=regimes[code],
                signal_decision_at=signal[0].decision_at if signal else None,
                total_score=signal[0].total_score if signal else None,
                technical_score=signal[1].get(Engine.TECHNICAL) if signal else None,
                fundamental_score=signal[1].get(Engine.FUNDAMENTAL) if signal else None,
                last_action=signal[0].action.value if signal else None,
                news_swept_at=swept.get(i),
            )
        )
    session.commit()
    logger.info(
        "watchlist %s: %d of %d names, %d left out",
        day,
        len(chosen.picks),
        len(pool_ids),
        chosen.left_out,
    )
    return snapshot


def members_since(session: Session, days: int) -> list[int]:
    """Every name on a morning list in the last `days` days: their days must be on record."""
    since = utc_now() - timedelta(days=days)
    rows = session.execute(
        select(WatchlistMember.instrument_id)
        .join(WatchlistSnapshot, WatchlistSnapshot.id == WatchlistMember.snapshot_id)
        .where(WatchlistSnapshot.asof >= since)
        .distinct()
    ).scalars()
    return list(rows)
