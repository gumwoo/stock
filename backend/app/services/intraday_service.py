"""Which names the minute bars are fetched for, and the analysis of each day that arrived.

**Targets.** The names in focus — tracked Korean names and the recent
candidates — every day, and every name on a morning list of the last ten days,
so each name the morning froze has its day on record even if one evening's
fetch failed.

**Analysis.** For each (name, day) whose minute bars came in whole, the day is
summarised (`app/scoring/intraday.py`) with its index over the same session:
from the index's own minutes when all 391 arrived, from the daily index bar
otherwise, and bucket by bucket only in the first case. A day that did not
come in whole is recorded with its status and no measures, and is analysed in
full once it does. Nothing is analysed on half a day.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.collectors.kis_minute import INDEX_MINUTES
from app.core.calendar import Market, MarketCalendar
from app.models import Instrument
from app.models.intraday import IntradaySummary
from app.models.watchlist import WatchlistMember, WatchlistSnapshot
from app.repositories import market_index_repo, minute_repo
from app.repositories.minute_repo import COMPLETE
from app.scoring.intraday import ANALYSIS_VERSION, Bar, DaySummary, bucket_of, index_day, summarize
from app.scoring.intraday_review import MemberDay, Result, evaluate
from app.scoring.watchlist import SELECTION_VERSION as WATCHLIST_SELECTION
from app.scoring.watchlist import STRATEGY_VERSION as WATCHLIST_STRATEGY
from app.services import llm_service, regime_service, watchlist_service

SEOUL = ZoneInfo("Asia/Seoul")
# Whole bars without an opening price to measure from: settled, not retried.
UNMEASURABLE = "UNMEASURABLE"
# Morning lists this recent keep their names on the fetch list.
WATCHLIST_DAYS = 10


def minute_targets(session: Session) -> list[int]:
    return sorted(
        set(llm_service.focus_ids(session))
        | set(watchlist_service.members_since(session, WATCHLIST_DAYS))
    )


def _local(t: datetime) -> time:
    return t.astimezone(SEOUL).time().replace(second=0, microsecond=0)


def _hhmm(t: time | None) -> str | None:
    return None if t is None else t.strftime("%H:%M")


def analyze(session: Session, *, instrument_ids: Collection[int] | None = None) -> dict[str, int]:
    """Summarise every fetched day not yet summarised in full under this version. Commits."""
    statuses = {
        key: value
        for key, value in minute_repo.latest_statuses(session).items()
        if instrument_ids is None or key[0] in instrument_ids
    }
    done = {
        (i, d): s
        for i, d, s in session.execute(
            select(
                IntradaySummary.instrument_id,
                IntradaySummary.session_date,
                IntradaySummary.status,
            ).where(IntradaySummary.analysis_version == ANALYSIS_VERSION)
        ).all()
    }
    calendar = MarketCalendar(Market.KR)
    instruments: dict[int, Instrument] = {}
    market_cache: dict[
        tuple[str, object], tuple[float | None, dict[time, float | None], str | None]
    ] = {}
    counts = {"complete": 0, "status_only": 0, "replaced": 0, "market_filled": 0}

    for (instrument_id, day), (status, _) in sorted(statuses.items()):
        previous = done.get((instrument_id, day))
        if previous in (COMPLETE, UNMEASURABLE, status):
            continue
        if previous is not None:
            # A day recorded as not whole, now arrived (or differently not whole).
            session.execute(
                delete(IntradaySummary).where(
                    IntradaySummary.instrument_id == instrument_id,
                    IntradaySummary.session_date == day,
                    IntradaySummary.analysis_version == ANALYSIS_VERSION,
                )
            )
            counts["replaced"] += 1
        if status != COMPLETE:
            session.add(
                IntradaySummary(
                    instrument_id=instrument_id,
                    session_date=day,
                    analysis_version=ANALYSIS_VERSION,
                    status=status,
                    bars=0,
                    buckets=[],
                )
            )
            counts["status_only"] += 1
            continue

        rows = minute_repo.bars_for(session, instrument_id, day)
        summary = summarize(
            [
                Bar(
                    _local(r.ts),
                    float(r.open),
                    float(r.high),
                    float(r.low),
                    float(r.close),
                    float(r.volume),
                )
                for r in rows
            ]
        )
        if instrument_id not in instruments:
            found = session.get(Instrument, instrument_id)
            assert found is not None
            instruments[instrument_id] = found
        inst = instruments[instrument_id]
        code = regime_service.index_for(inst.market, inst.listing)
        if (code, day) not in market_cache:
            market_cache[(code, day)] = _market(session, calendar, code, day)
        market, market_buckets, source = market_cache[(code, day)]
        session.add(
            _row(instrument_id, day, len(rows), summary, code, market, market_buckets, source)
        )
        counts["complete"] += 1
    counts["market_filled"] = _fill_market(session, calendar, instrument_ids)
    session.commit()
    return counts


def _fill_market(
    session: Session, calendar: MarketCalendar, instrument_ids: Collection[int] | None
) -> int:
    """Give a measured day its market once the index's day is on record.

    The evening's analysis runs before the evening's daily index bar arrives,
    so a day is often measured before its market can be. Only the market
    fields are filled in; the day's own measures stay as they were.
    """
    stmt = select(IntradaySummary).where(
        IntradaySummary.analysis_version == ANALYSIS_VERSION,
        IntradaySummary.status == COMPLETE,
        IntradaySummary.market_source.is_(None),
    )
    if instrument_ids is not None:
        stmt = stmt.where(IntradaySummary.instrument_id.in_(list(instrument_ids)))
    filled = 0
    cache: dict[tuple[str, object], tuple[float | None, dict[time, float | None], str | None]] = {}
    for row in session.execute(stmt).scalars():
        code = row.index_code
        if code is None:
            continue
        if (code, row.session_date) not in cache:
            cache[(code, row.session_date)] = _market(session, calendar, code, row.session_date)
        market, per_bucket, source = cache[(code, row.session_date)]
        if source is None:
            continue
        row.market_return_pct = market
        row.market_source = source
        row.buckets = [
            {**b, "market_return_pct": per_bucket.get(_parse_hhmm(str(b["start"])))}
            for b in row.buckets
        ]
        filled += 1
    return filled


def _parse_hhmm(hhmm: str) -> time:
    return time(int(hhmm[:2]), int(hhmm[3:5]))


def _market(
    session: Session, calendar: MarketCalendar, code: str, day: object
) -> tuple[float | None, dict[time, float | None], str | None]:
    minutes = minute_repo.index_bars_for(session, code, day)  # type: ignore[arg-type]
    if len(minutes) == INDEX_MINUTES:
        whole, per_bucket = index_day(
            [
                Bar(_local(b.ts), float(b.open), float(b.high), float(b.low), float(b.close), 0.0)
                for b in minutes
            ]
        )
        return whole, per_bucket, "INDEX_MINUTE"
    daily = market_index_repo.day_open_close(session, code, calendar.session_open(day))  # type: ignore[arg-type]
    if daily is None or daily[0] <= 0:
        return None, {}, None
    return (daily[1] / daily[0] - 1) * 100, {}, "DAILY"


def _row(
    instrument_id: int,
    day: object,
    bars: int,
    s: DaySummary | None,
    code: str,
    market: float | None,
    market_buckets: dict[time, float | None],
    source: str | None,
) -> IntradaySummary:
    if s is None:
        # Bars came in whole but carry no opening price to measure from.
        return IntradaySummary(
            instrument_id=instrument_id,
            session_date=day,
            analysis_version=ANALYSIS_VERSION,
            status=UNMEASURABLE,
            bars=bars,
            buckets=[],
        )
    return IntradaySummary(
        instrument_id=instrument_id,
        session_date=day,
        analysis_version=ANALYSIS_VERSION,
        status=COMPLETE,
        bars=s.bars,
        return_pct=s.return_pct,
        mfe_pct=s.mfe_pct,
        mae_pct=s.mae_pct,
        high_at=_hhmm(s.high_at),
        low_at=_hhmm(s.low_at),
        minutes_to_high=s.minutes_to_high,
        close_vs_vwap_pct=s.close_vs_vwap_pct,
        volatility_pct=s.volatility_pct,
        peak_volume_at=_hhmm(s.peak_volume_at),
        first_hour_pct=s.first_hour_pct,
        index_code=code,
        market_return_pct=market,
        market_source=source,
        buckets=[
            {
                "start": _hhmm(b.start),
                "return_pct": b.return_pct,
                "volume_share": b.volume_share,
                "bars": b.bars,
                "market_return_pct": market_buckets.get(b.start),
            }
            for b in s.buckets
        ],
    )


# --- the report ------------------------------------------------------------


@dataclass
class Baseline:
    """How these names' days usually go: every whole day on record, chosen or not."""

    name_days: int
    days: int
    volume_share: dict[str, float]
    abs_return: dict[str, float]
    high_bucket_share: dict[str, float]
    median_mfe: float | None
    median_mae: float | None


@dataclass
class IntradayReport:
    baseline: Baseline
    watchlist_days: int
    members: int
    missing: int
    """Members whose day has no whole summary yet."""
    results: list[Result] = field(default_factory=list)


def report(session: Session) -> IntradayReport:
    rows = list(
        session.execute(
            select(IntradaySummary).where(
                IntradaySummary.analysis_version == ANALYSIS_VERSION,
                IntradaySummary.status == COMPLETE,
            )
        ).scalars()
    )
    share: dict[str, list[float]] = defaultdict(list)
    moves: dict[str, list[float]] = defaultdict(list)
    highs: dict[str, int] = defaultdict(int)
    for r in rows:
        for b in r.buckets:
            start = str(b["start"])
            if isinstance(b.get("volume_share"), float | int):
                share[start].append(float(b["volume_share"]))  # type: ignore[arg-type]
            if isinstance(b.get("return_pct"), float | int):
                moves[start].append(abs(float(b["return_pct"])))  # type: ignore[arg-type]
        if r.high_at is not None:
            highs[str(bucket_label(r.high_at))] += 1
    baseline = Baseline(
        name_days=len(rows),
        days=len({r.session_date for r in rows}),
        volume_share={k: statistics.fmean(v) for k, v in sorted(share.items())},
        abs_return={k: statistics.fmean(v) for k, v in sorted(moves.items())},
        high_bucket_share={k: n / len(rows) for k, n in sorted(highs.items())} if rows else {},
        median_mfe=statistics.median([r.mfe_pct for r in rows if r.mfe_pct is not None])
        if rows
        else None,
        median_mae=statistics.median([r.mae_pct for r in rows if r.mae_pct is not None])
        if rows
        else None,
    )

    summary = {(r.instrument_id, r.session_date): r for r in rows}
    listed = session.execute(
        select(WatchlistMember, WatchlistSnapshot.session_date)
        .join(WatchlistSnapshot, WatchlistSnapshot.id == WatchlistMember.snapshot_id)
        .where(
            WatchlistSnapshot.strategy_version == WATCHLIST_STRATEGY,
            WatchlistSnapshot.selection_version == WATCHLIST_SELECTION,
        )
    ).all()
    members: list[MemberDay] = []
    missing = 0
    for m, day in listed:
        s = summary.get((m.instrument_id, day))
        if s is None or s.return_pct is None:
            missing += 1
            continue
        members.append(
            MemberDay(
                day=day,
                rank=m.rank,
                reasons=tuple(m.reasons),
                regime=m.regime,
                return_pct=s.return_pct,
                first_hour_pct=s.first_hour_pct,
                market_return_pct=s.market_return_pct,
            )
        )
    return IntradayReport(
        baseline=baseline,
        watchlist_days=len({day for _, day in listed}),
        members=len(members),
        missing=missing,
        results=evaluate(members),
    )


def bucket_label(hhmm: str) -> str:
    t = time(int(hhmm[:2]), int(hhmm[3:5]))
    return bucket_of(t).strftime("%H:%M")
