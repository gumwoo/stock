"""Finding untracked names the news has suddenly started writing about.

Every input is read as it stood at `asof`: verdicts through
`news_repo.confirmed_times`, which uses the verdict history rather than the
mention table; articles only if stored by then; and "untracked" as it was
then, which today's `tracked` flag cannot say on its own — a name promoted
after `asof` counts as untracked at it. Asking about the same moment twice
therefore gives the same list, however the rules or the watchlist have moved.

**Mentions are counted only inside what a sweep read.** Each company's
sweeps record the stretch of time they covered (`news_sweep_coverage`), and
both the counts and the days they are divided by come from those stretches.
A name with too little of either window read is reported as unmeasured
rather than ranked.

**A list made while the news had stopped says so.** The newest article stored
by `asof` is judged with `WallClockFreshnessRule`; a stale or missing feed
does not stop discovery, but the result carries the verdict, and a promotion
records it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.collectors.naver_news import NaverNewsCollector
from app.core.calendar import Market
from app.core.clock import ensure_utc, utc_now
from app.core.types import Freshness
from app.models import Listing
from app.models.news import NewsSource
from app.repositories import instrument_repo, news_repo, promotion_repo
from app.scoring.availability import WallClockFreshnessRule, evaluate_freshness
from app.scoring.discovery import MentionCounts, covered_days, inside, rank

DEFAULT_WINDOW = timedelta(hours=24)
DEFAULT_BASELINE = timedelta(days=14)
DEFAULT_TOP = 20
# Recent mentions a name needs before its surge is worth a look.
MIN_RECENT = 3
# How much of each window must have been read, in days, for a rate to mean
# anything. A busy name's one page can cover a couple of hours.
MIN_RECENT_DAYS = 0.25
MIN_BASELINE_DAYS = 1.0
# Sweeps run twice a day on weekdays, so the longest ordinary silence is
# Friday's afternoon sweep to Monday's morning one, about 64 hours.
NEWS_MAX_AGE = timedelta(hours=72)


@dataclass(frozen=True, slots=True)
class Candidate:
    instrument_id: int
    name: str
    symbol: str | None
    listing: Listing | None
    recent: int
    baseline: int
    recent_days: float
    baseline_days: float
    expected: float
    score: float


@dataclass(frozen=True, slots=True)
class Discovery:
    asof: datetime
    window: timedelta
    baseline: timedelta
    coverage_start: datetime | None
    newest_article: datetime | None
    freshness: Freshness
    considered: int
    unmeasured: int
    """Names with recent mentions but too little of a window read to rank."""
    candidates: list[Candidate]


def discover(
    session: Session,
    *,
    asof: datetime | None = None,
    window: timedelta = DEFAULT_WINDOW,
    baseline: timedelta = DEFAULT_BASELINE,
    top: int = DEFAULT_TOP,
    min_recent: int = MIN_RECENT,
) -> Discovery:
    asof = ensure_utc(asof, field="asof") if asof is not None else utc_now()
    source = NewsSource.NAVER_NEWS

    promoted_later = promotion_repo.promoted_after(session, asof)
    untracked = {
        i.instrument_id: i
        for i in instrument_repo.list_active(session, asof=asof.date(), market=Market.KR)
        if not i.tracked or i.instrument_id in promoted_later
    }

    recent_start = asof - window
    base_start = recent_start - baseline
    times = news_repo.confirmed_times(session, asof=asof, start=base_start, end=asof, source=source)
    read = news_repo.coverage(
        session,
        asof=asof,
        start=base_start,
        end=asof,
        collector=NaverNewsCollector.name,
        source=source,
    )

    counts: list[MentionCounts] = []
    for instrument_id in untracked:
        spans = read.get(instrument_id, [])
        recent_spans = [(max(lo, recent_start), hi) for lo, hi in spans if hi > recent_start]
        base_spans = [(lo, min(hi, recent_start)) for lo, hi in spans if lo < recent_start]
        seen = times.get(instrument_id, [])
        # Worth measuring only if enough was said at all; what counts is what
        # falls inside the stretches a sweep read.
        if sum(1 for t in seen if t > recent_start) < min_recent:
            continue
        counts.append(
            MentionCounts(
                instrument_id=instrument_id,
                recent=sum(1 for t in seen if t > recent_start and inside(t, recent_spans)),
                baseline=sum(1 for t in seen if t <= recent_start and inside(t, base_spans)),
                recent_days=covered_days(recent_spans, recent_start, asof),
                baseline_days=covered_days(base_spans, base_start, recent_start),
            )
        )

    coverage_start = news_repo.earliest_available_at(session, source=source, ingested_before=asof)
    newest = news_repo.latest_available_at(session, source=source, ingested_before=asof)
    freshness = evaluate_freshness(
        WallClockFreshnessRule(max_age=NEWS_MAX_AGE), now=asof, source_asof=newest
    ).freshness

    ranked, unmeasured = rank(
        counts,
        min_recent=min_recent,
        min_recent_days=MIN_RECENT_DAYS,
        min_baseline_days=MIN_BASELINE_DAYS,
        top=top,
    )
    candidates = [
        Candidate(
            instrument_id=s.instrument_id,
            name=untracked[s.instrument_id].name,
            symbol=instrument_repo.current_symbol(session, s.instrument_id),
            listing=untracked[s.instrument_id].listing,
            recent=s.recent,
            baseline=s.baseline,
            recent_days=s.recent_days,
            baseline_days=s.baseline_days,
            expected=s.expected,
            score=s.score,
        )
        for s in ranked
    ]
    return Discovery(
        asof=asof,
        window=window,
        baseline=baseline,
        coverage_start=coverage_start,
        newest_article=newest,
        freshness=freshness,
        considered=len(untracked),
        unmeasured=unmeasured,
        candidates=candidates,
    )
