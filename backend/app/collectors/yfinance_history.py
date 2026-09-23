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
from collections.abc import Collection
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
from app.models.instrument import Listing
from app.repositories import candle_repo, instrument_repo
from app.repositories.candle_repo import CandleRow

logger = logging.getLogger(__name__)

# yfinance addresses Korean listings by board: .KS for KOSPI, .KQ for KOSDAQ.
# A KOSDAQ ticker asked for with .KS returns an empty frame rather than an
# error, so getting this wrong looks like a company with no price history.
_YF_SUFFIX: dict[Market, str] = {Market.KR: ".KS", Market.US: ""}
_YF_LISTING: dict[Listing, str] = {
    Listing.KOSPI: ".KS",
    Listing.KOSDAQ: ".KQ",
    Listing.NYSE: "",
    Listing.NASDAQ: "",
}


def yf_ticker(symbol: str, market: Market, listing: Listing | None = None) -> str:
    """The ticker yfinance knows this instrument by.

    Falls back to the market default when the board is unknown, which is what
    every row seeded before the listing master had.
    """
    if listing is not None:
        return f"{symbol}{_YF_LISTING[listing]}"
    return f"{symbol}{_YF_SUFFIX[market]}"


class YFinanceHistoryCollector(BaseCollector):
    """Backfill daily bars for every active instrument."""

    name = "YFINANCE_HISTORY"

    def __init__(
        self, *, period: str = "2y", instrument_ids: Collection[int] | None = None
    ) -> None:
        self.period = period
        # Named instruments only, tracked or not: how a candidate gets its
        # prices before it is promoted, rather than after.
        self.instrument_ids = frozenset(instrument_ids) if instrument_ids is not None else None

    def collect(self, session: Session) -> CollectionResult:
        import yfinance as yf

        today = utc_now().date()
        if self.instrument_ids is not None:
            instruments = [
                i
                # Untracked on purpose: these are candidates, fetched so that
                # they can be promoted.
                for i in instrument_repo.list_active(session, asof=today, tracked=None)
                if i.instrument_id in self.instrument_ids
            ]
        else:
            # Tracked only: a name from the listing master has no reason to be
            # asked about, and asking is a network round trip each.
            instruments = instrument_repo.list_active(session, asof=today, tracked=True)
        if not instruments:
            return CollectionResult(detail="no active instruments to collect")

        read = saved = 0
        warnings: list[str] = []

        for instrument in instruments:
            symbol = instrument_repo.current_symbol(session, instrument.instrument_id)
            if symbol is None:
                warnings.append(f"instrument {instrument.instrument_id} has no current symbol")
                continue

            ticker = yf_ticker(symbol, instrument.market, instrument.listing)
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

            rows, before_calendar = self._to_rows(
                frame, instrument.instrument_id, calendar, now=utc_now()
            )
            if before_calendar:
                warnings.append(
                    f"{ticker}: {before_calendar} bars predate the {instrument.market} "
                    f"calendar ({calendar.first_session}) and were not stored"
                )
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
    def _to_rows(
        frame: object, instrument_id: int, calendar: MarketCalendar, *, now: datetime
    ) -> tuple[list[CandleRow], int]:
        """Convert a yfinance frame into candle rows anchored to session opens.

        Returns the rows and how many bars fell before the calendar begins.

        yfinance indexes daily bars by date in the exchange's local timezone.
        We re-anchor each bar to that session's actual opening instant in UTC,
        so a bar's timestamp means the same thing for KR and US alike, and
        record separately when the bar finished and became knowable.

        A bar whose session has not closed yet is skipped entirely. yfinance
        happily serves the day's partial bar during trading hours, and storing
        it would put an unfinished OHLCV in front of the scorer. The repository
        filters on availability as well, so this is the first of two guards.
        """
        rows: list[CandleRow] = []
        before_calendar = 0
        for index, row in frame.iterrows():  # type: ignore[attr-defined]
            day: date = index.date()
            if day < calendar.first_session:
                # `--period max` reaches past the loaded calendar: Apple listed
                # in 1980 and XNYS is loaded from 1990. A bar's `available_at`
                # is its session close, and there is no session to ask about,
                # so the bar cannot be given an honest availability at all.
                # Skipped and counted rather than dropped quietly, and counted
                # rather than fixed by widening the calendar — nothing here
                # needs 1980, and loading two more decades of XKRX to store
                # bars no backtest reaches would be paying for the wrong thing.
                before_calendar += 1
                continue
            if not calendar.is_session(day):
                # yfinance occasionally emits a bar for a non-session day.
                continue
            opened_at = calendar.session_open(day)
            available_at = calendar.bar_available_at(opened_at)
            if available_at > now:
                # The session is still running; this bar is not final.
                continue
            rows.append(
                CandleRow(
                    instrument_id=instrument_id,
                    interval=Interval.DAY_1,
                    ts=opened_at,
                    # A daily bar's close does not exist until the session ends.
                    available_at=available_at,
                    open=Decimal(str(round(float(row["Open"]), 6))),
                    high=Decimal(str(round(float(row["High"]), 6))),
                    low=Decimal(str(round(float(row["Low"]), 6))),
                    close=Decimal(str(round(float(row["Close"]), 6))),
                    volume=Decimal(str(round(float(row["Volume"]), 4))),
                    source="YFINANCE",
                )
            )
        return rows, before_calendar


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
