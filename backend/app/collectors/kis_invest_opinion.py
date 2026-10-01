"""KIS 종목투자의견(증권사 리포트의 의견·목표주가). 아침 목록 종목의 참고 표시용.

`GET /uapi/domestic-stock/v1/quotations/invest-opinion`, TR `FHKST663300C0`(한국투자증권 open-trading-api 예제).
2026-09-30 실측: 한 행 = 리포트 하나, 최신순, 한 번에 100행까지이고 연속 조회가 없다(tr_cont 빈 값). SK하이닉스 1년 조회가
100행에서 멈췄다(가장 오래된 행 2026-01-30). 그래서 기간을 183일로 잡고, 100행이 차고 가장 오래된 행이 기간 시작보다 늦으면
잘린 것(TRUNCATED)으로 적는다 — 최신순이라 잘리는 쪽은 오래된 리포트다. `mbcr_name`(증권사)은 예제 컬럼 표에 없지만
실측 응답에는 늘 있었다. 없으면 그 행을 건너뛴다.

평일 08:40(08:38 목록 확정 뒤, 08:44 아침 카톡 전)에 그날 목록 종목만 묻는다. 종목마다 조회 기록을 남겨 "리포트 없음"과 "못 받음"을 가른다.
목록 선정·채점에는 쓰지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.base import (
    BaseCollector,
    CollectionResult,
    SkipCollection,
    UpstreamUnavailableError,
    as_text,
)
from app.collectors.kis import KisClient
from app.collectors.kis_minute import kis_run_lock
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.models import WatchlistMember, WatchlistSnapshot
from app.models.analyst import FETCH_FAILED, FETCH_OK, FETCH_TRUNCATED
from app.repositories import analyst_repo, instrument_repo
from app.repositories.analyst_repo import OpinionRow
from app.scoring.analyst import WINDOW_DAYS
from app.scoring.watchlist import SELECTION_VERSION_V2, STRATEGY_VERSION_V2

PATH = "/uapi/domestic-stock/v1/quotations/invest-opinion"
TR = "FHKST663300C0"
PAGE_ROWS = 100
LOOKBACK_DAYS = 183


def _date(raw: str) -> date | None:
    # 날짜만(YYYYMMDD). 시각이 없으므로 시간대를 붙이지 않고 date로 바로 읽는다.
    if len(raw) != 8 or not raw.isascii() or not raw.isdigit():
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError:
        return None


def _price(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", "")) if raw else None
    except InvalidOperation:
        return None
    if value is None or not value.is_finite() or value <= 0:
        return None
    return value


def to_rows(output: Sequence[Any], instrument_id: int) -> tuple[list[OpinionRow], list[str]]:
    """KIS 행 → 저장 행. 날짜·증권사·의견이 없는 행은 건너뛰고 경고로 센다. 순수."""
    rows: list[OpinionRow] = []
    skipped = 0
    for r in output:
        if not isinstance(r, dict):
            skipped += 1
            continue
        day = _date(as_text(r, "stck_bsop_date"))
        broker = as_text(r, "mbcr_name")
        opinion = as_text(r, "invt_opnn")
        if day is None or not broker or not opinion:
            skipped += 1
            continue
        rows.append(
            OpinionRow(
                instrument_id=instrument_id,
                report_date=day,
                broker=broker[:40],
                opinion=opinion[:40],
                opinion_code=as_text(r, "invt_opnn_cls_code")[:4] or None,
                prior_opinion=as_text(r, "rgbf_invt_opnn")[:40] or None,
                target_price=_price(as_text(r, "hts_goal_prc")),
                prev_close=_price(as_text(r, "stck_prdy_clpr")),
            )
        )
    warnings = [f"{skipped} rows without a date, broker or opinion"] if skipped else []
    return rows, warnings


def fetch_status(count: int, oldest: date | None, start: date) -> str:
    """100행이 찼고 가장 오래된 행이 기간 시작보다 늦으면 잘린 것."""
    if count >= PAGE_ROWS and oldest is not None and oldest > start:
        return FETCH_TRUNCATED
    return FETCH_OK


def list_members(session: Session, days: Sequence[date]) -> list[tuple[date, int]]:
    """그날들 V2 목록 종목(날짜, 종목). 같은 종목은 가장 늦은 날짜 하나로."""
    rows = session.execute(
        select(WatchlistSnapshot.session_date, WatchlistMember.instrument_id)
        .join(WatchlistMember, WatchlistMember.snapshot_id == WatchlistSnapshot.id)
        .where(
            WatchlistSnapshot.session_date.in_(list(days)),
            WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
            WatchlistSnapshot.selection_version == SELECTION_VERSION_V2,
        )
    ).all()
    latest: dict[int, date] = {}
    for d, i in rows:
        latest[i] = max(d, latest.get(i, d))
    return sorted(((d, i) for i, d in latest.items()), key=lambda x: (x[0], x[1]))


class KisInvestOpinionCollector(BaseCollector):
    """목록 종목의 증권사 투자의견을 받아 둔다."""

    name = "KIS_INVEST_OPINION"

    def __init__(
        self,
        *,
        days: Sequence[date] | None = None,
        end: date | None = None,
        only_missing: bool = False,
        client: KisClient | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        # days: 목록 날짜들(기본은 오늘). end: 조회 끝 날짜(기본은 오늘). only_missing: 그날 창을 덮는 조회가 이미
        # 있는 종목은 건너뛴다(저녁 보충).
        self.days = list(days) if days is not None else None
        self.end = end
        self.only_missing = only_missing
        self._client = client
        self._now = now

    def is_configured(self) -> bool:
        return get_settings().kis_enabled

    def skip_reason(self) -> str:
        return "set KIS_APP_KEY and KIS_APP_SECRET (a KIS Open API app key)"

    def collect(self, session: Session) -> CollectionResult:
        now = self._now()
        calendar = MarketCalendar(Market.KR)
        today = calendar.local_today(now)
        days = self.days if self.days is not None else [today]
        members = list_members(session, days)
        if not members:
            raise SkipCollection(f"no morning list for {', '.join(map(str, days))}")
        if self.only_missing:
            members = [
                (d, i)
                for d, i in members
                if i
                not in analyst_repo.usable_fetches(
                    session, [i], day=d, window_start=d - timedelta(days=WINDOW_DAYS)
                )
            ]
            if not members:
                return CollectionResult(detail="every list name already fetched")
        with kis_run_lock():
            return self._collect(session, members, end=self.end or today, now=now)

    def _collect(
        self, session: Session, members: list[tuple[date, int]], *, end: date, now: datetime
    ) -> CollectionResult:
        client = self._client or KisClient()
        start = end - timedelta(days=LOOKBACK_DAYS)
        read = saved = 0
        warnings: list[str] = []
        truncated = failed = 0
        try:
            for _, instrument_id in members:
                code = instrument_repo.current_symbol(session, instrument_id)
                if not code:
                    warnings.append(f"{instrument_id}: no code")
                    continue
                try:
                    body, _ = client.get(
                        PATH,
                        tr_id=TR,
                        params={
                            "fid_cond_mrkt_div_code": "J",
                            "fid_cond_scr_div_code": "16633",
                            "fid_input_iscd": code,
                            "fid_input_date_1": start.strftime("%Y%m%d"),
                            "fid_input_date_2": end.strftime("%Y%m%d"),
                        },
                    )
                    if str(body.get("rt_cd")) != "0":
                        raise UpstreamUnavailableError(f"{code}: {body.get('msg1')}")
                    output = body.get("output") or []
                    if not isinstance(output, list):
                        raise UpstreamUnavailableError(f"{code}: output is not a list")
                except UpstreamUnavailableError as exc:
                    # 한 종목 실패는 그 종목만: 조회 기록에 FAILED로 남겨 화면이 "없음"으로 읽지 않게 한다.
                    failed += 1
                    warnings.append(str(exc))
                    analyst_repo.record_fetch(
                        session,
                        instrument_id,
                        fetched_at=now,
                        start=start,
                        end=end,
                        status=FETCH_FAILED,
                        rows=0,
                        oldest=None,
                    )
                    continue
                rows, warn = to_rows(output, instrument_id)
                warnings += [f"{code}: {w}" for w in warn]
                oldest = min((r.report_date for r in rows), default=None)
                status = fetch_status(len(output), oldest, start)
                truncated += status == FETCH_TRUNCATED
                read += len(output)
                saved += analyst_repo.save_opinions(session, rows)
                analyst_repo.record_fetch(
                    session,
                    instrument_id,
                    fetched_at=now,
                    start=start,
                    end=end,
                    status=status,
                    rows=len(rows),
                    oldest=oldest,
                )
                # 종목마다 남긴다: 한도·잠금 문제로 도중에 멈춰도 앞서 받은 종목은 기록에 있다.
                session.commit()
        finally:
            if self._client is None:
                client.close()
        session.commit()
        detail = f"{len(members)} names, {start}..{end}: {saved} new reports"
        if truncated:
            detail += f"; {truncated} truncated at {PAGE_ROWS} rows"
        if failed:
            detail += f"; {failed} failed"
        return CollectionResult(
            items_read=read,
            items_saved=saved,
            partial=bool(failed or warnings),
            warnings=warnings,
            detail=detail,
        )
