"""Daily bars of the market indexes, from yfinance.

KOSPI (^KS11), KOSDAQ (^KQ11) and the S&P 500 (^GSPC): one call each, raw
closes, anchored to the session calendar the same way company bars are. Used
only to say what kind of market a signal was made in (`app/scoring/regime.py`);
nothing is scored on them.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.collectors.base import BaseCollector, CollectionResult, UpstreamUnavailableError
from app.collectors.yfinance_history import YFinanceHistoryCollector
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.repositories import market_index_repo
from app.repositories.market_index_repo import IndexBarRow

INDEXES: dict[str, Market] = {
    "^KS11": Market.KR,
    "^KQ11": Market.KR,
    "^GSPC": Market.US,
}

# 밤사이 미국 반도체(화면 참고용). 국면 계산에는 쓰지 않는다 — `INDEXES`와 따로 둔다.
# NVDA·MU는 주식이라 분할이 있으면 yfinance의 분할 조정 값과 예전에 저장한 값이 섞일 수 있다(저장은 한 세션 한 행,
# 재수집은 무시). 그래서 등락을 보일 때 크게 튀면 숨긴다(`overnight_service`).
OVERNIGHT_REFERENCES: dict[str, Market] = {
    "^SOX": Market.US,
    "NVDA": Market.US,
    "MU": Market.US,
}


class MarketIndexCollector(BaseCollector):
    """Daily index bars for every market the system scores."""

    name = "MARKET_INDEX"

    def __init__(self, *, period: str = "2y", codes: dict[str, Market] | None = None) -> None:
        # Two years: the regime's volatility rank looks back a year of
        # 20-session windows, 271 sessions in all.
        self.period = period
        self.codes = codes if codes is not None else INDEXES

    def collect(self, session: Session) -> CollectionResult:
        import yfinance as yf

        read = saved = 0
        warnings: list[str] = []
        for code, market in self.codes.items():
            try:
                frame = yf.Ticker(code).history(
                    period=self.period, interval="1d", auto_adjust=False
                )
            except Exception as exc:
                raise UpstreamUnavailableError(f"yfinance failed for {code}: {exc}") from exc
            if frame.empty:
                warnings.append(f"{code}: no data returned")
                continue
            bars, _ = YFinanceHistoryCollector._to_rows(
                frame, 0, MarketCalendar(market), now=utc_now()
            )
            rows = [
                IndexBarRow(
                    index_code=code,
                    ts=b["ts"],
                    available_at=b["available_at"],
                    open=b["open"],
                    high=b["high"],
                    low=b["low"],
                    close=b["close"],
                )
                for b in bars
            ]
            read += len(rows)
            saved += market_index_repo.save_bars(session, rows)
        session.commit()
        return CollectionResult(
            items_read=read,
            items_saved=saved,
            partial=bool(warnings),
            warnings=warnings,
            detail=f"{', '.join(self.codes)} period={self.period}: {read} bars, {saved} new",
        )


class UsSemiReferenceCollector(MarketIndexCollector):
    """밤사이 미국 반도체 지표(^SOX·NVDA·MU) 일봉. 이름을 따로 두어 지수 수집의 성공·실패와 섞이지 않게 한다."""

    # `MARKET_INDEX`로 시작하지 않는 이름(수집 기록 조회가 이름 앞부분으로 찾는다).
    name = "US_SEMI_REFERENCE"

    def __init__(self, *, period: str = "3mo") -> None:
        super().__init__(period=period, codes=OVERNIGHT_REFERENCES)
