"""Daily bars and FX rates from yfinance.

Two jobs, both of which exist because of Toss's limits rather than in spite of
them:

1. **History.** Toss serves 1-minute and daily bars only, and not far back. A
   backtest needs years, so long history is backfilled from here.
2. **Working without credentials.** Toss requires a registered client and a
   whitelisted IP. Until those exist this collector is what puts real prices on
   the dashboard, so the system is useful before any key is obtained.

yfinance is an unofficial scraper of a public endpoint. It is treated as such:
every call is wrapped, failures are typed, and nothing here is load-bearing for
correctness. When Toss credentials appear, Toss becomes the source for recent
bars and this stays as the historical tail.

**Adjusted prices are refused.** `auto_adjust=False` is passed explicitly and
raw OHLC is stored. Adjusted series silently rewrite history every time a
dividend is paid, which would make a stored bar disagree with itself between
runs — and reproducibility is the point of the whole design.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.collectors.base import (
    BaseCollector,
    CollectionResult,
    UpstreamUnavailableError,
)
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.models import FxRate, Interval
from app.repositories import candle_repo, instrument_repo
from app.repositories.candle_repo import CandleRow

logger = logging.getLogger(__name__)

# yfinance addresses Korean listings with a .KS suffix for KOSPI.
_YF_SUFFIX: dict[Market, str] = {Market.KR: ".KS", Market.US: ""}


def yf_ticker(symbol: str, market: Market) -> str:
    return f"{symbol}{_YF_SUFFIX[market]}"


class YFinanceHistoryCollector(BaseCollector):
    """Backfill daily bars for every active instrument."""

    name = "YFINANCE_HISTORY"

    def __init__(self, *, period: str = "2y") -> None:
        self.period = period

    def collect(self, session: Session) -> CollectionResult:
        import yfinance as yf

        today = utc_now().date()
        instruments = instrument_repo.list_active(session, asof=today)
        if not instruments:
            return CollectionResult(detail="no active instruments to collect")

        read = saved = 0
        warnings: list[str] = []

        for instrument in instruments:
            symbol = instrument_repo.current_symbol(session, instrument.instrument_id)
            if symbol is None:
                warnings.append(f"instrument {instrument.instrument_id} has no current symbol")
                continue

            ticker = yf_ticker(symbol, instrument.market)
            calendar = MarketCalendar(instrument.market)

            try:
                frame = yf.Ticker(ticker).history(
                    period=self.period,
                    interval="1d",
                    auto_adjust=False,  # raw bars only; adjustments derive at read time
                )
            except Exception as exc:
                raise UpstreamUnavailableError(f"yfinance failed for {ticker}: {exc}") from exc

            if frame.empty:
                warnings.append(f"{ticker}: no data returned")
                continue

            rows = self._to_rows(frame, instrument.instrument_id, calendar)
            read += len(rows)
            saved += candle_repo.save_revisions(session, rows)

        session.commit()
        return CollectionResult(
            items_read=read,
            items_saved=saved,
            partial=bool(warnings),
            warnings=warnings,
            detail=f"{len(instruments)} instruments, period={self.period}",
        )

    @staticmethod
    def _to_rows(frame: object, instrument_id: int, calendar: MarketCalendar) -> list[CandleRow]:
        """Convert a yfinance frame into candle rows anchored to session opens.

        yfinance indexes daily bars by date in the exchange's local timezone.
        We re-anchor each bar to that session's actual opening instant in UTC,
        so a bar's timestamp means the same thing for KR and US alike, and
        record separately when the bar finished and became knowable.
        """
        rows: list[CandleRow] = []
        for index, row in frame.iterrows():  # type: ignore[attr-defined]
            day: date = index.date()
            if not calendar.is_session(day):
                # yfinance occasionally emits a bar for a non-session day.
                continue
            opened_at = calendar.session_open(day)
            rows.append(
                CandleRow(
                    instrument_id=instrument_id,
                    interval=Interval.DAY_1,
                    ts=opened_at,
                    # A daily bar's close does not exist until the session ends.
                    available_at=calendar.bar_available_at(opened_at),
                    open=Decimal(str(round(float(row["Open"]), 6))),
                    high=Decimal(str(round(float(row["High"]), 6))),
                    low=Decimal(str(round(float(row["Low"]), 6))),
                    close=Decimal(str(round(float(row["Close"]), 6))),
                    volume=Decimal(str(round(float(row["Volume"]), 4))),
                    source="YFINANCE",
                )
            )
        return rows


class FxRateCollector(BaseCollector):
    """Daily USD/KRW rates.

    Needed to value US holdings in KRW and, more importantly, to split a
    position's profit into the part that came from the stock and the part that
    came from the currency. Without it a US return reported in KRW cannot be
    interpreted.
    """

    name = "FX_RATE"

    def __init__(self, *, period: str = "2y", pair: tuple[str, str] = ("USD", "KRW")) -> None:
        self.period = period
        self.base, self.quote = pair

    def collect(self, session: Session) -> CollectionResult:
        import yfinance as yf

        ticker = f"{self.base}{self.quote}=X"
        try:
            frame = yf.Ticker(ticker).history(period=self.period, interval="1d")
        except Exception as exc:
            raise UpstreamUnavailableError(f"yfinance FX failed for {ticker}: {exc}") from exc

        if frame.empty:
            raise UpstreamUnavailableError(f"no FX data returned for {ticker}")

        # A daily close is not knowable until the day ends. Stamping it
        # available at 00:00 of its own date would let a valuation use a rate
        # that had not been set yet — the same look-ahead the three signal
        # clocks exist to prevent, and it would be careless to guard it for
        # equities and not for the currency they are converted through.
        # So a day's close becomes available at the start of the next day.
        existing = {
            (r.rate_date)
            for r in session.query(FxRate)
            .filter(FxRate.base == self.base, FxRate.quote == self.quote)
            .all()
        }

        read = saved = 0
        for index, row in frame.iterrows():
            day: date = index.date()
            read += 1
            if day in existing:
                continue
            session.add(
                FxRate(
                    base=self.base,
                    quote=self.quote,
                    rate_date=day,
                    rate=Decimal(str(round(float(row["Close"]), 8))),
                    source="YFINANCE",
                    available_at=datetime(day.year, day.month, day.day, tzinfo=UTC)
                    + timedelta(days=1),
                )
            )
            saved += 1

        session.commit()
        return CollectionResult(
            items_read=read,
            items_saved=saved,
            detail=f"{self.base}/{self.quote} period={self.period}",
        )
