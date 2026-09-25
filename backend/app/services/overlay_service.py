"""Assembling the news-event overlay at a moment, and filing it beside a signal.

The arithmetic lives in `app.scoring.overlay`; this module only gathers its
inputs as they stood at `asof` and records the result. Every input is
point-in-time: readings made by then, of articles available and stored by
then, whose relevance verdict at that moment was CONFIRMED. So an overlay
asked for again about the same moment comes out the same after a re-judgment,
a new sweep or a later reading.

DART event disclosures join the readings (overlay version 2): a buyback or
a contract classified from the filing's title, clustered with the articles
about the same event so it counts once.

Two things are recorded with it because the number alone would overstate
itself. `unread_articles` is how many confirmed articles in the lookback had
no reading yet — reading runs within the subscription's limits and lags the
news, so a small overlay may mean little was read rather than little
happened. `news_freshness` is whether the news pipe was flowing at `asof`
(`WallClockFreshnessRule`); a quiet overlay while the pipe was down says
nothing.

Failures here do not reach the base score. The signal is already committed
when the overlay is computed, and an error is logged and the overlay left out.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.calendar import Market
from app.core.clock import ensure_utc
from app.core.types import Freshness
from app.models import Signal, SignalOverlay
from app.models.news import NewsSource
from app.repositories import disclosure_repo, instrument_repo, llm_repo, news_repo
from app.scoring.availability import WallClockFreshnessRule, evaluate_freshness
from app.scoring.disclosure_events import RULE_CONFIDENCE, classify
from app.scoring.overlay import EventReading, Overlay, OverlayParams, compute_overlay
from app.services.discovery_service import NEWS_MAX_AGE
from app.services.llm_service import SENTIMENT_PROMPT_VERSION

logger = logging.getLogger(__name__)

PARAMS = OverlayParams()


@dataclass(frozen=True, slots=True)
class OverlayAt:
    instrument_id: int
    asof: datetime
    overlay: Overlay
    unread_articles: int
    news_freshness: Freshness
    model: str
    prompt_version: int
    params: OverlayParams


def overlays_at(
    session: Session,
    *,
    asof: datetime,
    instrument_ids: Collection[int],
    params: OverlayParams = PARAMS,
) -> dict[int, OverlayAt]:
    asof = ensure_utc(asof, field="asof")
    model = get_settings().sentiment_llm_model
    readings, unread = llm_repo.readings_asof(
        session,
        asof=asof,
        since=asof - params.lookback(),
        model=model,
        prompt_version=SENTIMENT_PROMPT_VERSION,
        instrument_ids=instrument_ids,
    )
    newest = news_repo.latest_available_at(
        session, source=NewsSource.NAVER_NEWS, ingested_before=asof
    )
    flowing = evaluate_freshness(
        WallClockFreshnessRule(max_age=NEWS_MAX_AGE), now=asof, source_asof=newest
    ).freshness
    # Naver's news covers Korean listings only. For any other market there is
    # no feed at all, and a zero overlay there means nothing was collected,
    # not that nothing happened.
    markets = {
        i.instrument_id: i.market
        for i in (instrument_repo.get_by_id(session, n) for n in instrument_ids)
        if i is not None
    }

    by_instrument: dict[int, list[EventReading]] = {i: [] for i in instrument_ids}
    for r in readings:
        by_instrument.setdefault(r.instrument_id, []).append(
            EventReading(
                news_item_id=r.news_item_id,
                available_at=r.available_at,
                event_type=r.event_type,
                sentiment=r.sentiment,
                intensity=r.intensity,
                confidence=r.confidence,
                title=r.title,
            )
        )
    # DART's event disclosures, classified from their titles. Same bounds as
    # the readings: available and stored by `asof`.
    for d in disclosure_repo.disclosures_asof(
        session, asof=asof, since=asof - params.lookback(), instrument_ids=instrument_ids
    ):
        event = classify(d.report_nm)
        if event is None:
            continue
        by_instrument.setdefault(d.instrument_id, []).append(
            EventReading(
                news_item_id=d.id,
                available_at=d.available_at,
                event_type=event.event_type,
                sentiment=event.sentiment if event.sentiment is not None else 0.0,
                intensity=event.intensity,
                confidence=RULE_CONFIDENCE,
                title=f"[공시] {d.report_nm}",
                source="DART",
                directional=event.sentiment is not None,
            )
        )
    return {
        instrument_id: OverlayAt(
            instrument_id=instrument_id,
            asof=asof,
            overlay=compute_overlay(found, asof=asof, params=params),
            unread_articles=unread.get(instrument_id, 0),
            news_freshness=flowing
            if markets.get(instrument_id) is Market.KR
            else Freshness.MISSING,
            model=model,
            prompt_version=SENTIMENT_PROMPT_VERSION,
            params=params,
        )
        for instrument_id, found in by_instrument.items()
    }


def _detail(result: OverlayAt, limit: int = 10) -> list[dict[str, object]]:
    return [
        {
            "event_type": c.event_type,
            "first_at": c.first_at.isoformat(),
            "articles": c.articles,
            "disclosures": len(c.disclosure_ids),
            "sentiment": round(c.sentiment, 3),
            "intensity": round(c.intensity, 3),
            "confidence": round(c.confidence, 3),
            "decay": round(c.decay, 3),
            "contribution": round(c.contribution, 4),
            "title": c.title[:200],
        }
        for c in result.overlay.clusters[:limit]
    ]


def attach(session: Session, signal: Signal) -> SignalOverlay | None:
    """File the overlay at the signal's decision moment beside it. Commits.

    `decision_at` rather than the time scoring ran: the signal is the
    judgement finalised at that moment, and news that arrived after it was not
    part of it.
    """
    try:
        result = overlays_at(
            session, asof=signal.decision_at, instrument_ids=[signal.instrument_id]
        )[signal.instrument_id]
        row = SignalOverlay(
            signal_id=signal.id,
            asof=result.asof,
            overlay_version=result.params.version,
            reading_model=result.model,
            reading_prompt_version=result.prompt_version,
            points=result.overlay.points,
            raw=result.overlay.raw,
            events=len(result.overlay.clusters),
            readings_used=result.overlay.readings_used,
            unread_articles=result.unread_articles,
            news_freshness=result.news_freshness,
            detail=_detail(result),
        )
        session.add(row)
        session.commit()
        return row
    except Exception:
        # The signal is committed and correct without this; losing the
        # overlay is a gap to log, not a reason to lose the score.
        session.rollback()
        logger.exception("overlay for signal %s was not recorded", signal.id)
        return None
