"""장 시작 직후 등락률 순위와 동반주 알림(2026-10-12부터, 표시·기록 전용).

거래일 개장 1~10분 매분(워커가 9·10시 1~10분에 부르고, 여기서 실제 개장 시각 기준으로 거른다):
1. KIS 등락률 순위 `GET /uapi/domestic-stock/v1/ranking/fluctuation`, TR `FHPST01700000`을 시장별(KOSPI "0001"·KOSDAQ "1001")로
   한 번씩. `fid_prc_cls_code="1"`(종가 대비)이면 전일 대비 등락률 내림차순 30행이다(2026-10-12 실측 — "0"은 저가 대비라 순서가 다르다).
   마스터에 있는 보통주만 `open_mover`에 남긴다(ETF·ETN·스팩·우선주는 마스터에 없어 빠진다 — 빠진 수를 detail에 남긴다).
   거래량이 모두 0이면(달력이 모르는 늦은 개장) 그 분을 건너뛴다.
2. 등락률 +7% 이상이고 관계(이름 묶음·AI)가 있는 이끄는 종목마다 동반 후보를 고르고, 순위에 없는 후보는 KIS 현재가
   (`inquire-price`, TR `FHKST01010100`)로 한 분에 최대 8개만 조회한다(KIS 속도 0.5회/초 — 순위 2회 + 현재가 8회 ≈ 20초).
   `companion_alert`에 그 분의 후보와 등락률·조회 시각을 남긴다.

관계는 워커가 `companions`로 넘긴다(서비스 계층을 여기서 부르지 않으려고). 다른 KIS 실행이 잠금을 쥐고 있으면 그 분은 SKIPPED로 남는다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.collectors.base import (
    BaseCollector,
    CollectionResult,
    SkipCollection,
    UpstreamUnavailableError,
)
from app.collectors.kis import KisClient
from app.collectors.kis_minute import kis_run_lock
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.models import CompanionAlert, OpenMover
from app.repositories import instrument_repo
from app.scoring import companions as cp

RANK_PATH = "/uapi/domestic-stock/v1/ranking/fluctuation"
RANK_TR = "FHPST01700000"
PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
PRICE_TR = "FHKST01010100"
MARKETS = {"KOSPI": "0001", "KOSDAQ": "1001"}
MAX_PRICE_CALLS = 8

Companions = Callable[[Session, Sequence[int]], Mapping[int, Sequence[cp.Related]]]


def rank_params(iscd: str) -> dict[str, str]:
    return {
        "fid_cond_mrkt_div_code": "J",
        "fid_cond_scr_div_code": "20170",
        "fid_input_iscd": iscd,
        "fid_rank_sort_cls_code": "0",  # 상승률순
        "fid_input_cnt_1": "0",
        "fid_prc_cls_code": "1",  # 종가 대비(전일 대비 등락률) — "0"은 저가 대비
        "fid_input_price_1": "",
        "fid_input_price_2": "",
        "fid_vol_cnt": "",
        "fid_trgt_cls_code": "0",
        "fid_trgt_exls_cls_code": "0",
        "fid_div_cls_code": "0",
        "fid_rsfl_rate1": "",
        "fid_rsfl_rate2": "",
    }


class KisOpenMoverCollector(BaseCollector):
    name = "KIS_OPEN_MOVERS"

    def __init__(
        self,
        *,
        companions: Companions,
        client: KisClient | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._companions = companions
        self._client = client
        self._now = now

    def is_configured(self) -> bool:
        return get_settings().kis_enabled

    def skip_reason(self) -> str:
        return "set KIS_APP_KEY and KIS_APP_SECRET (a KIS Open API app key)"

    def collect(self, session: Session) -> CollectionResult:
        start = self._now()
        calendar = MarketCalendar(Market.KR)
        day = calendar.local_today(start)
        if not calendar.is_session(day):
            raise SkipCollection(f"{day} is not a session")
        if not cp.in_window(start, calendar.session_open(day)):
            raise SkipCollection("outside the first ten minutes after the open")
        with kis_run_lock():
            return self._collect(session, day, start)

    def _collect(self, session: Session, day: Any, start: datetime) -> CollectionResult:
        client = self._client or KisClient()
        warnings: list[str] = []
        unmapped = 0
        try:
            movers: dict[int, cp.Mover] = {}
            fetched: dict[int, datetime] = {}
            rows: list[dict[str, Any]] = []
            all_movers: list[cp.Mover] = []
            for listing, iscd in MARKETS.items():
                body, _ = client.get(RANK_PATH, tr_id=RANK_TR, params=rank_params(iscd))
                if str(body.get("rt_cd")) != "0":
                    raise UpstreamUnavailableError(f"KIS fluctuation {listing}: {body.get('msg1')}")
                got = self._now()
                parsed = cp.parse_ranking(body.get("output") or [])
                all_movers += parsed
                for rank, m in enumerate(parsed, 1):
                    inst = instrument_repo.resolve_symbol(session, m.code, Market.KR, asof=day)
                    if inst is None:
                        unmapped += 1
                        continue
                    movers.setdefault(inst.instrument_id, m)
                    fetched.setdefault(inst.instrument_id, got)
                    rows.append(
                        {
                            "session_date": day,
                            "at": start,
                            "fetched_at": got,
                            "instrument_id": inst.instrument_id,
                            "market_rank": rank,
                            "change_pct": m.change_pct,
                            "price": m.price,
                            "open_change_pct": m.open_change_pct,
                            "volume": m.volume,
                        }
                    )
            if not cp.traded(all_movers):
                raise SkipCollection("no trades in the ranking yet (the market has not opened)")
            if rows:
                session.execute(pg_insert(OpenMover).values(rows).on_conflict_do_nothing())

            hot = [i for i, m in movers.items() if m.change_pct >= cp.LEADER_AT]
            related = self._companions(session, hot) if hot else {}
            picks = cp.leaders(movers, related)
            changes: dict[int, float | None] = {i: m.change_pct for i, m in movers.items()}
            prices: dict[int, float | None] = {i: m.price for i, m in movers.items()}
            calls = 0
            for pick in picks:
                # 화면에 남을 수 있는 후보(출처가 많은 것부터 최대 6개)에만 현재가를 쓴다.
                likely = sorted(pick.companions, key=lambda r: -len(r.sources))[: cp.MAX_COMPANIONS]
                for r in likely:
                    i = r.instrument_id
                    if i in changes or calls >= MAX_PRICE_CALLS:
                        continue
                    code = instrument_repo.current_symbol(session, i)
                    if not code:
                        continue
                    calls += 1
                    try:
                        body, _ = client.get(
                            PRICE_PATH,
                            tr_id=PRICE_TR,
                            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
                        )
                    except UpstreamUnavailableError as exc:
                        warnings.append(f"{code}: {exc}")
                        continue
                    out = body.get("output") or {}
                    changes[i] = _num(out.get("prdy_ctrt"))
                    prices[i] = _num(out.get("stck_prpr"))
                    fetched[i] = self._now()
            alerts = []
            for pick in picks:
                for r, change in cp.order_companions(pick.companions, changes):
                    alerts.append(
                        {
                            "session_date": day,
                            "at": start,
                            "leader_id": pick.leader_id,
                            "leader_change": pick.leader_change,
                            "companion_id": r.instrument_id,
                            "companion_change": change,
                            "companion_price": prices.get(r.instrument_id),
                            "companion_fetched_at": fetched.get(r.instrument_id),
                            "sources": list(r.sources),
                            "reason": (r.reason or None) and r.reason[:200],
                        }
                    )
            if alerts:
                session.execute(pg_insert(CompanionAlert).values(alerts).on_conflict_do_nothing())
            session.commit()
        finally:
            if self._client is None:
                client.close()
        detail = (
            f"{len(rows)} movers ({unmapped} not in the master), {len(picks)} leaders, "
            f"{len(alerts)} companion rows, {calls} price calls"
        )
        return CollectionResult(
            items_read=len(rows) + unmapped,
            items_saved=len(rows) + len(alerts),
            detail=detail,
            warnings=warnings,
        )


def _num(raw: Any) -> float | None:
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None
