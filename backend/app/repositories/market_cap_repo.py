"""시가총액 순위: 세션별 저장과, 목록 날 기준 비중 조회."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.instrument import Listing
from app.models.market import MarketCapRank
from app.repositories import bulk


class MarketCapRow(NamedTuple):
    session_date: date
    listing: Listing
    rank: int
    code: str
    instrument_id: int | None
    name: str
    market_cap_eok: Decimal
    weight_pct: float
    close: Decimal
    listed_shares: Decimal


def save_ranks(session: Session, rows: Sequence[MarketCapRow]) -> int:
    """넣거나 고친다(같은 세션의 끝난 값은 같다). commit하지 않는다."""
    written = 0
    for batch in bulk.batched(rows, columns=len(MarketCapRow._fields)):
        stmt = pg_insert(MarketCapRank).values([r._asdict() for r in batch])
        stmt = stmt.on_conflict_do_update(
            index_elements=["session_date", "listing", "code"],
            set_={
                "rank": stmt.excluded.rank,
                "instrument_id": stmt.excluded.instrument_id,
                "name": stmt.excluded.name,
                "market_cap_eok": stmt.excluded.market_cap_eok,
                "weight_pct": stmt.excluded.weight_pct,
                "close": stmt.excluded.close,
                "listed_shares": stmt.excluded.listed_shares,
            },
        )
        written += len(session.execute(stmt.returning(MarketCapRank.id)).scalars().all())
    return written


def latest_day(
    session: Session, *, before: date | None = None, on_or_after: date | None = None
) -> date | None:
    """`before`보다 이른 가장 최근 순위표 날짜, 또는 `on_or_after` 이후 가장 이른 날짜."""
    q = select(func.max(MarketCapRank.session_date))
    if before is not None:
        q = q.where(MarketCapRank.session_date < before)
    if on_or_after is not None:
        q = select(func.min(MarketCapRank.session_date)).where(
            MarketCapRank.session_date >= on_or_after
        )
    return session.execute(q).scalar_one_or_none()


def weights_on(
    session: Session, day: date, instrument_ids: Collection[int]
) -> dict[int, tuple[float, Listing]]:
    """그 세션 순위표에서 종목별 (비중, 시장). 순위표에 없는 종목은 빠진다 — 상위 30 밖이면 대형주가 아니다."""
    if not instrument_ids:
        return {}
    rows = session.execute(
        select(MarketCapRank.instrument_id, MarketCapRank.weight_pct, MarketCapRank.listing).where(
            MarketCapRank.session_date == day, MarketCapRank.instrument_id.in_(list(instrument_ids))
        )
    ).all()
    return {i: (w, listing) for i, w, listing in rows if i is not None}
