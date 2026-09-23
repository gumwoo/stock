"""Promoting a discovered name: fetch its data first, then follow it.

The order is the point. `tracked` is what puts a name into scoring and into
every peer group, so a name marked tracked before its prices arrive would sit
in both with nothing behind it. Data is fetched for the named candidates
only, tracked or not, and a candidate is promoted only when daily bars
actually landed.

Financial facts are fetched and counted but not required. Some listings file
nothing DART can parse into the ratios — SPACs, REITs, recent listings — and
scoring already stands the fundamental factor down when facts are missing,
with the reason recorded. The count goes on the promotion row, so a name
promoted without them is visible as such.

The price history reaches as far back as the filings do (`PERIOD` and
`YEARS_BACK` match), for the reason the CLI's `PERIODS` table gives: a rule
backtested on ten years of prices and five of filings scores the earlier half
on technicals alone.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.collectors.base import run_collector
from app.collectors.dart_fundamental import DartFundamentalCollector
from app.collectors.yfinance_history import YFinanceHistoryCollector
from app.models import Interval
from app.models.collector import CollectorStatus
from app.repositories import candle_repo, fundamental_repo, promotion_repo
from app.repositories.promotion_repo import PromotionRow
from app.services.discovery_service import Candidate, Discovery

PERIOD = "5y"
YEARS_BACK = 5

# Fetches data for the given instruments and returns the run's status.
Fetch = Callable[[Session, Sequence[int]], CollectorStatus]


def fetch_prices(session: Session, instrument_ids: Sequence[int]) -> CollectorStatus:
    run = run_collector(
        YFinanceHistoryCollector(period=PERIOD, instrument_ids=instrument_ids), session
    )
    return run.status


def fetch_fundamentals(session: Session, instrument_ids: Sequence[int]) -> CollectorStatus:
    run = run_collector(
        DartFundamentalCollector(years_back=YEARS_BACK, instrument_ids=instrument_ids), session
    )
    return run.status


@dataclass(frozen=True, slots=True)
class Outcome:
    instrument_id: int
    name: str
    promoted: bool
    candle_bars: int
    fundamental_facts: int
    reason: str


def promote(
    session: Session,
    discovery: Discovery,
    candidates: Sequence[Candidate],
    *,
    prices: Fetch = fetch_prices,
    fundamentals: Fetch = fetch_fundamentals,
) -> list[Outcome]:
    """Fetch data for `candidates`, then promote those whose prices arrived.

    Commits. The collectors commit their own rows as they go; the promotions
    are committed together at the end.
    """
    if not candidates:
        return []
    ids = [c.instrument_id for c in candidates]
    price_status = prices(session, ids)
    fundamentals_status = fundamentals(session, ids)

    outcomes: list[Outcome] = []
    for c in candidates:
        bars = candle_repo.count_for(session, c.instrument_id, Interval.DAY_1)
        facts = fundamental_repo.count_for(session, c.instrument_id)
        if bars == 0:
            outcomes.append(
                Outcome(
                    c.instrument_id,
                    c.name,
                    promoted=False,
                    candle_bars=0,
                    fundamental_facts=facts,
                    reason=f"no daily bars (price run {price_status.value})",
                )
            )
            continue
        promotion_repo.promote(
            session,
            PromotionRow(
                instrument_id=c.instrument_id,
                discovered_asof=discovery.asof,
                window_hours=int(discovery.window.total_seconds() // 3600),
                baseline_days=c.baseline_days,
                recent_mentions=c.recent,
                baseline_mentions=c.baseline,
                score=c.score,
                news_freshness=discovery.freshness.value,
                candle_bars=bars,
                fundamental_facts=facts,
            ),
        )
        reason = (
            "promoted"
            if facts
            else (f"promoted without financial facts (DART run {fundamentals_status.value})")
        )
        outcomes.append(
            Outcome(
                c.instrument_id,
                c.name,
                promoted=True,
                candle_bars=bars,
                fundamental_facts=facts,
                reason=reason,
            )
        )
    session.commit()
    return outcomes
