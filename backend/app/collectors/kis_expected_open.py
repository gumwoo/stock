"""9시 전 예상체결가: 장전 동시호가의 예상 시가를 기록하고, 갭 +3% 이상 종목을 08:54 카톡에 참고로 알린다.

**2026-10-12부터 목록에서 빼지 않는다**(소유자 결정: 10/8 BGF에코머티리얼즈, 10/12 금호전기·금호건설처럼 큰 갭 뒤 더 오른
종목을 뺐다). 그 전(10/6~10/12)에는 판정으로 남은 행에 GAP_UP을 붙였고, 그 기록은 그대로 둔다. 반대 근거도 남긴다: 3개월 공시
표본에서 갭 +3% 이상은 -1.38%(t -3.9), 우리 목록 8일 25건은 +2.5% 익절 -1.60%였다.

`GET /uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn`, TR `FHKST01010200`(주식현재가 호가/예상체결,
한국투자증권 open-trading-api 예제). 2026-10-05(휴장일) 실측: `output2`가 dict이고 `antc_cnpr`(예상 체결가), `stck_sdpr`(기준가),
`antc_cntg_prdy_ctrt`(예상 체결 전일 대비율), `antc_vol`, `antc_mkop_cls_code`가 있다. 장전 동시호가 시간의 값은 첫 실행에서 확인한다.

- **판정(`decide`, 평일 08:50):** 그날 목록 40개를 모두 조회해 남긴다(연구 기록). 목록 행은 바꾸지 않는다. 그날 조회가 돌았는지,
  판정 상한(08:54) 안에 끝났는지(`applied` — 카톡이 이 실행의 값을 읽는다), 판정할 수 있는 값 중 +3% 이상인 수(`gap_up`,
  2026-10-12 전에는 "뺀 수"였다)를 목록 기록의 `inputs.gap_check`에 남긴다(`excludes: false`).
- **기록(`record`, 평일 08:57):** 목록에 남은 종목만 조회해 남긴다. 판정은 바꾸지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
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
from app.models import ExpectedOpen, WatchlistMember, WatchlistSnapshot
from app.repositories import instrument_repo
from app.scoring import gap
from app.scoring.gap import DECIDE_BY
from app.scoring.watchlist import SELECTION_VERSIONS_V2, STRATEGY_VERSION_V2

PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
TR = "FHKST01010200"
SEOUL = ZoneInfo("Asia/Seoul")
DECIDE = "decide"
RECORD = "record"
STOP_AT = time(9, 0)


class KisExpectedOpenCollector(BaseCollector):
    name = "KIS_EXPECTED_OPEN"

    def __init__(
        self,
        *,
        purpose: str = DECIDE,
        client: KisClient | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.purpose = purpose
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
        if start.astimezone(SEOUL).time() >= STOP_AT:
            raise SkipCollection("past 09:00; the expected open no longer means anything")
        snap = session.execute(
            select(WatchlistSnapshot).where(
                WatchlistSnapshot.session_date == day,
                WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2,
                WatchlistSnapshot.selection_version.in_(SELECTION_VERSIONS_V2),
            )
        ).scalar_one_or_none()
        if snap is None:
            raise SkipCollection(f"no morning list for {day}")
        members = list(
            session.execute(
                select(WatchlistMember)
                .where(WatchlistMember.snapshot_id == snap.id)
                .order_by(WatchlistMember.rank)
            ).scalars()
        )
        if self.purpose == RECORD:
            members = [m for m in members if m.excluded_reason is None]
        with kis_run_lock():
            return self._collect(session, snap, members, start=start)

    def _collect(
        self,
        session: Session,
        snap: WatchlistSnapshot,
        members: list[WatchlistMember],
        *,
        start: datetime,
    ) -> CollectionResult:
        client = self._client or KisClient()
        quotes: dict[int, gap.Quote] = {}
        failed = 0
        stopped = False
        warnings: list[str] = []
        try:
            for m in members:
                if self._now().astimezone(SEOUL).time() >= STOP_AT:
                    warnings.append("stopped at 09:00")
                    stopped = True
                    break
                code = instrument_repo.current_symbol(session, m.instrument_id)
                if not code:
                    failed += 1
                    continue
                try:
                    body, _ = client.get(
                        PATH,
                        tr_id=TR,
                        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
                    )
                except UpstreamUnavailableError as exc:
                    failed += 1
                    warnings.append(f"{code}: {exc}")
                    continue
                raw = body.get("output2")
                q = gap.parse(raw if isinstance(raw, dict) else None)
                if q is None:
                    failed += 1
                    warnings.append(f"{code}: no output2")
                    continue
                quotes[m.instrument_id] = q
                session.add(_row(snap, m.instrument_id, q, raw, start, self._now(), self.purpose))
            session.commit()
        finally:
            if self._client is None:
                client.close()

        out = {
            "at": start.isoformat(),
            "purpose": self.purpose,
            "checked": len(quotes),
            "failed": failed,
            "judgeable": sum(1 for q in quotes.values() if q.judgeable),
            "mismatched": sum(1 for q in quotes.values() if q.mismatched),
        }
        if self.purpose == DECIDE:
            # 목록 행은 바꾸지 않는다(2026-10-12부터). applied는 "08:54 안에 끝나 카톡이 이 실행을 읽는다"는 뜻으로만 남는다.
            applied = self._now().astimezone(SEOUL).time() < DECIDE_BY
            # 40개 전부에서 센다(점수·전일 급등으로 뺀 종목 포함). 카톡의 "N개"는 남은 종목만 센다.
            gap_up_n = sum(1 for q in quotes.values() if gap.gap_up(q))
            out.update({"applied": applied, "gap_up": gap_up_n, "excludes": False})
            # JSON 칸은 변경 추적이 없다: 새 dict를 대입해야 저장된다. 이미 반영된 판정을 늦은 재실행(반영 안 함)이
            # 덮지 않게 한다.
            earlier = (snap.inputs or {}).get("gap_check")
            if applied or not (isinstance(earlier, dict) and earlier.get("applied")):
                snap.inputs = {**(snap.inputs or {}), "gap_check": out}
            session.commit()
        return CollectionResult(
            items_read=len(members),
            items_saved=len(quotes),
            detail=", ".join(f"{k} {v}" for k, v in out.items() if k != "at"),
            warnings=warnings,
            partial=failed > 0 or stopped,
        )


def _row(
    snap: WatchlistSnapshot,
    instrument_id: int,
    q: gap.Quote,
    raw: Any,
    check_at: datetime,
    fetched_at: datetime,
    purpose: str,
) -> ExpectedOpen:
    def dec(v: float | None) -> Decimal | None:
        return None if v is None else Decimal(str(v))

    return ExpectedOpen(
        session_date=snap.session_date,
        instrument_id=instrument_id,
        check_at=check_at,
        purpose=purpose,
        fetched_at=fetched_at,
        expected_price=dec(q.expected_price),
        base_price=dec(q.base_price),
        change_pct=q.change_pct,
        reported_pct=q.reported_pct,
        expected_volume=q.volume,
        mkop_code=q.mkop_code,
        judgeable=q.judgeable,
        raw=raw if isinstance(raw, dict) else None,
    )
