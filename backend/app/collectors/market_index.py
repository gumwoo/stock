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


class MarketIndexCollector(BaseCollector):
    """Daily index bars for every market the system scores."""

    name = "MARKET_INDEX"

    def __init__(self, *, period: str = "2y") -> None:
        # Two years: the regime's volatility rank looks back a year of
        # 20-session windows, 271 sessions in all.
        self.period = period

    def collect(self, session: Session) -> CollectionResult:
        import yfinance as yf

        read = saved = 0
        warnings: list[str] = []
        for code, market in INDEXES.items():
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
            detail=f"{', '.join(INDEXES)} period={self.period}: {read} bars, {saved} new",
        )
