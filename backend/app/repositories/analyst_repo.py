"""증권사 투자의견 저장·조회. commit하지 않는다(수집기가 끝에서 한다)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.analyst import FETCH_FAILED, AnalystOpinion, AnalystOpinionFetch
from app.repositories import bulk


class OpinionRow(NamedTuple):
    instrument_id: int
    report_date: date
    broker: str
    opinion: str
    opinion_code: str | None
    prior_opinion: str | None
    target_price: Decimal | None
    prev_close: Decimal | None


def save_opinions(session: Session, rows: Sequence[OpinionRow]) -> int:
    """이미 있는 리포트는 건너뛴다. 새로 넣은 행 수."""
    written = 0
    for batch in bulk.batched(rows, columns=len(OpinionRow._fields)):
        stmt = pg_insert(AnalystOpinion).values([r._asdict() for r in batch])
        stmt = stmt.on_conflict_do_nothing(constraint="uq_analyst_opinion_report")
        written += len(session.execute(stmt.returning(AnalystOpinion.id)).scalars().all())
    return written


def record_fetch(
    session: Session,
    instrument_id: int,
    *,
    fetched_at: datetime,
    start: date,
    end: date,
    status: str,
    rows: int,
    oldest: date | None,
) -> None:
    session.add(
        AnalystOpinionFetch(
            instrument_id=instrument_id,
            fetched_at=fetched_at,
            start_date=start,
            end_date=end,
            status=status,
            rows=rows,
            oldest=oldest,
        )
    )


def usable_fetches(
    session: Session, instrument_ids: Collection[int], *, day: date, window_start: date
) -> dict[int, AnalystOpinionFetch]:
    """목록 날 `day`의 창([window_start, day))을 덮는 성공한 조회 중 가장 최근 것. 없으면 그 종목은 빠진다."""
    if not instrument_ids:
        return {}
    rows = (
        session.execute(
            select(AnalystOpinionFetch)
            .where(
                AnalystOpinionFetch.instrument_id.in_(list(instrument_ids)),
                AnalystOpinionFetch.status != FETCH_FAILED,
                AnalystOpinionFetch.start_date <= window_start,
                # 목록 날까지 물어본 조회만. 전날 아침 조회는 전날 늦게 올라온 리포트를 모르므로, 그날 조회가 실패했을 때
                # 저녁 보충이 그 조회를 "이미 받음"으로 보고 건너뛰면 안 된다.
                AnalystOpinionFetch.end_date >= day,
            )
            .order_by(AnalystOpinionFetch.fetched_at)
        )
        .scalars()
        .all()
    )
    return {r.instrument_id: r for r in rows}  # fetched_at 순이라 마지막이 최신


def reports(
    session: Session, instrument_ids: Collection[int], *, start: date, before: date
) -> dict[int, list[AnalystOpinion]]:
    """[start, before) 날짜의 리포트, 종목별 날짜·id 순."""
    out: dict[int, list[AnalystOpinion]] = {}
    if not instrument_ids:
        return out
    for r in session.execute(
        select(AnalystOpinion)
        .where(
            AnalystOpinion.instrument_id.in_(list(instrument_ids)),
            AnalystOpinion.report_date >= start,
            AnalystOpinion.report_date < before,
        )
        .order_by(AnalystOpinion.report_date, AnalystOpinion.id)
    ).scalars():
        out.setdefault(r.instrument_id, []).append(r)
    return out
