"""Korean financial statements from OpenDART.

The same point-in-time machinery as SEC, fed by a source shaped very
differently.

**What DART gives that SEC does not.** Statements are structured from the
start, so there is no pre-XBRL coverage gap — the "the market had this and we
cannot read it" case does not arise for Korean filings. Accounts carry IFRS
taxonomy identifiers (`ifrs-full_Revenue`), so mapping is on stable ids rather
than on Korean labels that vary between filers.

**What it does not give: dates.** A row says `제 57 기` and nothing more. Period
boundaries are derived from `bsns_year` and the company's `acc_mt` — its fiscal
year-end month, from `company.json`. That derivation is the one genuinely
fragile step here, so it is isolated in `fiscal_period_bounds` and tested
directly.

**Three years per response.** Each row carries `thstrm` (current), `frmtrm`
(prior) and `bfefrmtrm` (the one before), all under one filing. That yields
restatement history for free and in the same shape SEC comparatives do: a
fiscal year reappears in each subsequent annual report, and if the figure
changed, that is a new revision under a new receipt number.

`rcept_no` doubles as the accession and the filing date — its first eight
digits are `YYYYMMDD`. The availability rule is identical to SEC's, because
DART has the same limitation: `rcept_dt` is a date with no time of day, so a
filing is usable from the next session's open.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.collectors.base import (
    BaseCollector,
    CollectionResult,
    RateLimitedError,
    TokenBucket,
    UpstreamUnavailableError,
)
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.models.fundamental import FiscalPeriod, FundamentalSource
from app.repositories import filing_repo, fundamental_repo, instrument_repo
from app.repositories.filing_repo import FilingRow
from app.repositories.fundamental_repo import FundamentalRow

logger = logging.getLogger(__name__)

BASE = "https://opendart.fss.or.kr/api"

# DART account ids mapped onto the concept names the engine already speaks, so
# a Korean filing and a US one produce the same series. The ids are IFRS
# taxonomy identifiers (or DART extensions where IFRS has no equivalent), which
# are stable in a way the Korean labels are not.
ACCOUNT_MAP: dict[str, str] = {
    "ifrs-full_Revenue": "Revenues",
    "ifrs-full_ProfitLoss": "NetIncomeLoss",
    "dart_OperatingIncomeLoss": "OperatingIncomeLoss",
    "ifrs-full_BasicEarningsLossPerShare": "EarningsPerShareBasic",
    "ifrs-full_DilutedEarningsLossPerShare": "EarningsPerShareDiluted",
    "ifrs-full_Assets": "Assets",
    "ifrs-full_Liabilities": "Liabilities",
    "ifrs-full_Equity": "StockholdersEquity",
    "ifrs-full_CashAndCashEquivalents": "CashAndCashEquivalentsAtCarryingValue",
}

# Concepts measured at an instant rather than across a span. The distinction
# decides whether a fact gets a period_start, and getting it wrong would let a
# balance be compared against a flow.
INSTANTANEOUS = frozenset(
    {"Assets", "Liabilities", "StockholdersEquity", "CashAndCashEquivalentsAtCarryingValue"}
)

PER_SHARE = frozenset({"EarningsPerShareBasic", "EarningsPerShareDiluted"})

ANNUAL_REPORT = "11011"  # 사업보고서

# Which response column belongs to which year, counting back from bsns_year.
PERIOD_COLUMNS = (
    ("thstrm_amount", 0),
    ("frmtrm_amount", 1),
    ("bfefrmtrm_amount", 2),
)


def fiscal_period_bounds(business_year: int, fiscal_end_month: int) -> tuple[date, date]:
    """The span a Korean annual report covers.

    DART states no dates, only `제 57 기`, so the period is reconstructed from
    the business year and the company's fiscal year-end month. Most Korean
    filers close in December, but not all, and assuming December would silently
    shift every period for the ones that do not.

    Returns (period_start, period_end), both inclusive.
    """
    if not 1 <= fiscal_end_month <= 12:
        raise ValueError(f"fiscal_end_month must be 1-12, got {fiscal_end_month}")

    last_day = calendar.monthrange(business_year, fiscal_end_month)[1]
    period_end = date(business_year, fiscal_end_month, last_day)

    # The year opens the month after it closes. A December filer runs Jan-Dec
    # of the business year; a March filer runs April of the year before through
    # March of it.
    start_month = fiscal_end_month % 12 + 1
    start_year = business_year if fiscal_end_month == 12 else business_year - 1
    period_start = date(start_year, start_month, 1)

    return period_start, period_end


def filed_date_from_receipt(rcept_no: str) -> date | None:
    """DART receipt numbers begin with the filing date as YYYYMMDD."""
    if len(rcept_no) < 8 or not rcept_no[:8].isdigit():
        return None
    try:
        return date(int(rcept_no[:4]), int(rcept_no[4:6]), int(rcept_no[6:8]))
    except ValueError:
        return None


class DartFundamentalCollector(BaseCollector):
    """Fetch Korean annual statements and store every reported revision."""

    name = "DART_FUNDAMENTAL"

    def __init__(self, *, years_back: int = 5) -> None:
        settings = get_settings()
        self._key = settings.dart_api_key
        # DART publishes no per-second limit, only a daily quota. A modest
        # bucket keeps us from hammering a public service.
        self._bucket = TokenBucket(2.0)
        self.years_back = years_back

    def is_configured(self) -> bool:
        return bool(self._key)

    def skip_reason(self) -> str:
        return "set DART_API_KEY to enable (free from opendart.fss.or.kr)"

    # --- transport --------------------------------------------------------

    def _get(self, client: httpx.Client, path: str, **params: str) -> dict[str, Any]:
        self._bucket.acquire()
        try:
            response = client.get(
                f"{BASE}/{path}", params={"crtfc_key": self._key, **params}, timeout=60
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"DART request failed for {path}: {exc}") from exc

        if response.status_code != 200:
            raise UpstreamUnavailableError(f"DART returned {response.status_code} for {path}")

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise UpstreamUnavailableError(f"DART returned non-JSON for {path}") from exc

        status = payload.get("status")
        if status == "020":
            raise RateLimitedError("DART daily quota exhausted")
        if status == "013":
            # No data for this company and year. A normal answer, not a fault —
            # a company simply may not have filed for that year.
            return {"status": status, "list": []}
        if status != "000":
            raise UpstreamUnavailableError(
                f"DART {path} returned status {status}: {payload.get('message')}"
            )
        return payload

    def _fiscal_end_month(self, client: httpx.Client, corp_code: str) -> int:
        """The company's fiscal year-end month, without which dates are guesses."""
        payload = self._get(client, "company.json", corp_code=corp_code)
        raw = str(payload.get("acc_mt") or "").strip()
        if not raw.isdigit():
            raise UpstreamUnavailableError(
                f"DART gave no usable acc_mt for {corp_code}; fiscal period dates "
                "cannot be derived and guessing December would shift every period"
            )
        return int(raw)

    # --- collection -------------------------------------------------------

    def collect(self, session: Session) -> CollectionResult:
        today = utc_now().date()
        instruments = [
            i
            for i in instrument_repo.list_active(session, asof=today)
            if i.market is Market.KR and i.kr_corp_code
        ]
        if not instruments:
            return CollectionResult(detail="no Korean instruments with a DART corp code")

        krx = MarketCalendar(Market.KR)
        read = saved = 0
        warnings: list[str] = []

        with httpx.Client() as client:
            for instrument in instruments:
                corp_code = str(instrument.kr_corp_code)
                fiscal_end_month = self._fiscal_end_month(client, corp_code)

                saved += filing_repo.save_filings(
                    session, self._collect_filings(client, corp_code, instrument.instrument_id, krx)
                )

                for year in range(today.year, today.year - self.years_back, -1):
                    payload = self._get(
                        client,
                        "fnlttSinglAcntAll.json",
                        corp_code=corp_code,
                        bsns_year=str(year),
                        reprt_code=ANNUAL_REPORT,
                        fs_div="CFS",
                    )
                    items = payload.get("list") or []
                    if not items:
                        continue

                    rows, seen = self._to_rows(
                        items,
                        instrument_id=instrument.instrument_id,
                        business_year=year,
                        fiscal_end_month=fiscal_end_month,
                        calendar=krx,
                    )
                    read += seen
                    saved += fundamental_repo.save_facts(session, rows)

        session.commit()
        return CollectionResult(
            items_read=read,
            items_saved=saved,
            partial=bool(warnings),
            warnings=warnings,
            detail=f"{len(instruments)} instruments, {self.years_back} years each",
        )

    def _collect_filings(
        self,
        client: httpx.Client,
        corp_code: str,
        instrument_id: int,
        calendar: MarketCalendar,
    ) -> list[FilingRow]:
        """Periodic disclosures, so absence can be told from non-publication."""
        payload = self._get(
            client,
            "list.json",
            corp_code=corp_code,
            bgn_de="19990101",
            end_de=utc_now().date().strftime("%Y%m%d"),
            pblntf_ty="A",  # 정기공시
            page_count="100",
        )

        rows: list[FilingRow] = []
        for item in payload.get("list") or []:
            rcept_no = str(item.get("rcept_no") or "")
            filed_at = filed_date_from_receipt(rcept_no)
            if filed_at is None:
                continue
            rows.append(
                FilingRow(
                    instrument_id=instrument_id,
                    form=str(item.get("report_nm") or "").strip(),
                    filed_at=filed_at,
                    period_of_report=None,
                    available_at=calendar.next_session_open(filed_at),
                    accession=rcept_no,
                    source=FundamentalSource.DART,
                )
            )
        return rows

    def _to_rows(
        self,
        items: list[dict[str, Any]],
        *,
        instrument_id: int,
        business_year: int,
        fiscal_end_month: int,
        calendar: MarketCalendar,
    ) -> tuple[list[FundamentalRow], int]:
        """Flatten three reported years out of each account row."""
        rows: list[FundamentalRow] = []
        seen = 0

        for item in items:
            concept = ACCOUNT_MAP.get(str(item.get("account_id") or ""))
            if concept is None:
                continue

            rcept_no = str(item.get("rcept_no") or "")
            filed_at = filed_date_from_receipt(rcept_no)
            if filed_at is None:
                continue

            currency = str(item.get("currency") or "KRW").strip() or "KRW"
            unit = f"{currency}/shares" if concept in PER_SHARE else currency

            for column, years_back in PERIOD_COLUMNS:
                seen += 1
                amount = _parse_amount(item.get(column))
                if amount is None:
                    continue

                year = business_year - years_back
                try:
                    period_start, period_end = fiscal_period_bounds(year, fiscal_end_month)
                except ValueError:
                    continue

                rows.append(
                    FundamentalRow(
                        instrument_id=instrument_id,
                        taxonomy="dart",
                        concept=concept,
                        unit=unit,
                        # A balance is measured at the period end and carries no
                        # span; conflating the two would let it be divided by a
                        # flow from a different length of time.
                        period_start=None if concept in INSTANTANEOUS else period_start,
                        period_end=period_end,
                        fiscal_year=year,
                        fiscal_period=FiscalPeriod.FY,
                        form="사업보고서",
                        value=amount,
                        filed_at=filed_at,
                        # Same rule as SEC: a filing date has no time of day, so
                        # the honest boundary is the next session's open.
                        available_at=calendar.next_session_open(filed_at),
                        accession=rcept_no,
                        source=FundamentalSource.DART,
                    )
                )

        return rows, seen


def _parse_amount(raw: object) -> Decimal | None:
    """DART amounts are comma-grouped strings, blank when not reported."""
    text = str(raw or "").strip().replace(",", "")
    if not text or text == "-":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None
