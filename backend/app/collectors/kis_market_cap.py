"""KIS 시가총액 상위(보통주, KOSPI·KOSDAQ 각 상위 30). 지수 대형주 표시용.

`GET /uapi/domestic-stock/v1/ranking/market-cap`, TR `FHPST01740000`(한국투자증권 open-trading-api 예제). 날짜 인자가 없어
부르는 순간의 값을 준다. 그래서 세션 날짜는 "마지막으로 끝난 한국 세션"으로 정하고, 장중에는 받지 않는다(장중 값을 그날
종가로 적지 않게). 평일 16:10에 받는다 — 15:40 지수 분봉 뒤, 16:20 분봉 수집 앞이라 KIS 한 번에 하나 잠금이 비어 있다.

2026-09-28 장 마감 뒤 첫 호출: 거래소 30행, 연속 조회 없음. 삼성전자 25.69%, SK하이닉스 20.91%.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

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
from app.models.instrument import Listing
from app.repositories import instrument_repo, market_cap_repo
from app.repositories.market_cap_repo import MarketCapRow

PATH = "/uapi/domestic-stock/v1/ranking/market-cap"
TR = "FHPST01740000"
# KIS 시장 코드: 0001 거래소(KOSPI), 1001 코스닥.
MARKETS: dict[Listing, str] = {Listing.KOSPI: "0001", Listing.KOSDAQ: "1001"}
MAX_PAGES = 3
CROSS_CHECK = 0.05  # 종가 곱하기 상장주식수와 시가총액이 이 비율 넘게 어긋나면 경고


def finished_session(calendar: MarketCalendar, now: datetime) -> date | None:
    """마지막으로 끝난 한국 세션. 오늘이 세션인데 아직 안 끝났으면(새벽·장중) None — 받지 않는다."""
    today = calendar.local_today(now)
    if calendar.is_session(today):
        return today if calendar.has_closed(now) else None
    earlier = calendar.sessions_between(today - timedelta(days=14), today - timedelta(days=1))
    return earlier[-1] if earlier else None


def _decimal(row: Mapping[str, Any], key: str) -> Decimal:
    raw = as_text(row, key)
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise UpstreamUnavailableError(f"KIS market cap {key}={raw!r}") from None
    if not value.is_finite() or value < 0:
        raise UpstreamUnavailableError(f"KIS market cap {key}={raw!r}")
    return value


def to_rows(
    output: Sequence[Mapping[str, Any]],
    listing: Listing,
    day: date,
    resolve: Callable[[str], int | None],
) -> tuple[list[MarketCapRow], list[str]]:
    """응답 행을 저장할 행으로. 순수(코드→종목 id는 넘겨받는다). 두 번째 값은 교차 점검 경고."""
    rows: list[MarketCapRow] = []
    warnings: list[str] = []
    for r in output:
        code = as_text(r, "mksc_shrn_iscd")
        if not code:
            continue
        cap = _decimal(r, "stck_avls")
        close = _decimal(r, "stck_prpr")
        shares = _decimal(r, "lstn_stcn")
        weight = float(_decimal(r, "mrkt_whol_avls_rlim"))
        try:
            rank = int(as_text(r, "data_rank"))
        except ValueError:
            raise UpstreamUnavailableError(
                f"KIS market cap data_rank={as_text(r, 'data_rank')!r}"
            ) from None
        # 시가총액 단위가 억원인지 스스로 점검한다: 종가 곱하기 상장주식수 / 1억.
        implied = close * shares / Decimal(100_000_000)
        if cap > 0 and abs(implied - cap) / cap > Decimal(str(CROSS_CHECK)):
            warnings.append(f"{code} cap {cap} vs price*shares {implied:.0f}")
        rows.append(
            MarketCapRow(
                day,
                listing,
                rank,
                code,
                resolve(code),
                as_text(r, "hts_kor_isnm"),
                cap,
                weight,
                close,
                shares,
            )
        )
    return rows, warnings


class KisMarketCapCollector(BaseCollector):
    """장 마감 뒤 시가총액 순위를 받아 둔다."""

    name = "KIS_MARKET_CAP"

    def __init__(
        self, *, client: KisClient | None = None, now: Callable[[], datetime] = utc_now
    ) -> None:
        self._client = client
        self._now = now

    def is_configured(self) -> bool:
        return get_settings().kis_enabled

    def skip_reason(self) -> str:
        return "set KIS_APP_KEY and KIS_APP_SECRET (a KIS Open API app key)"

    def collect(self, session: Session) -> CollectionResult:
        day = finished_session(MarketCalendar(Market.KR), self._now())
        if day is None:
            raise SkipCollection(
                "the Korean session has not finished; a ranking now would be intraday"
            )
        with kis_run_lock():
            return self._collect(session, day)

    def _collect(self, session: Session, day: date) -> CollectionResult:
        client = self._client or KisClient()
        read = saved = 0
        warnings: list[str] = []

        def resolve(code: str) -> int | None:
            inst = instrument_repo.resolve_symbol(session, code, Market.KR, asof=day)
            return inst.instrument_id if inst else None

        try:
            for listing, iscd in MARKETS.items():
                cont = ""
                for _ in range(MAX_PAGES):
                    body, cont = client.get(
                        PATH,
                        tr_id=TR,
                        tr_cont="N" if cont in ("M", "F") else "",
                        params={
                            "fid_cond_mrkt_div_code": "J",
                            "fid_cond_scr_div_code": "20174",
                            "fid_div_cls_code": "1",  # 보통주
                            "fid_input_iscd": iscd,
                            "fid_trgt_cls_code": "0",
                            "fid_trgt_exls_cls_code": "0",
                            "fid_input_price_1": "",
                            "fid_input_price_2": "",
                            "fid_vol_cnt": "",
                        },
                    )
                    if str(body.get("rt_cd")) != "0":
                        raise UpstreamUnavailableError(
                            f"KIS market cap {listing}: {body.get('msg1')}"
                        )
                    output = body.get("output") or []
                    rows, warn = to_rows(output, listing, day, resolve)
                    read += len(output)
                    warnings += warn
                    saved += market_cap_repo.save_ranks(session, rows)
                    if cont not in ("M", "F"):
                        break
        finally:
            if self._client is None:
                client.close()
        session.commit()
        detail = f"{day}: {saved} rows"
        if warnings:
            detail += "; cross-check: " + "; ".join(warnings[:5])
        return CollectionResult(
            items_read=read, items_saved=saved, partial=bool(warnings), detail=detail
        )
