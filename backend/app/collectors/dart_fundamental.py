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
import re
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
    as_object,
    as_rows,
    as_text,
)
from app.collectors.quota import QuotaGuard
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.models.fundamental import FiscalPeriod, FundamentalSource
from app.repositories import filing_repo, fundamental_repo, instrument_repo
from app.repositories.filing_repo import FilingRow
from app.repositories.fundamental_repo import FundamentalRow

logger = logging.getLogger(__name__)

BASE = "https://opendart.fss.or.kr/api"

# The same ledger group the listing master uses. One published DART cap,
# shared by every endpoint we call against it.
QUOTA_GROUP = "dart"

# DART account ids mapped onto the concept names the engine already speaks, so
# a Korean filing and a US one produce the same series. The ids are IFRS
# taxonomy identifiers (or DART extensions where IFRS has no equivalent), which
# are stable in a way the Korean labels are not.
#
# **Both namespace spellings, because the prefix changed and the filings did
# not.** The IFRS taxonomy moved from `ifrs` to `ifrs-full` with the 2018
# edition, and DART's full-statement endpoint returns whichever the filing
# used. Samsung's business years 2015 through 2018 come back as `ifrs_Revenue`
# and `ifrs_ProfitLoss`; 2019 onwards as `ifrs-full_Revenue` and
# `ifrs-full_ProfitLoss`. Mapping only the newer spelling silently dropped
# eight of the nine concepts for every year before 2019 — and left the ninth,
# because `dart_OperatingIncomeLoss` is a DART extension that never moved.
#
# The result was a Korean history that looked four years longer than it was.
# Every one of those years held exactly one figure, none of them a figure the
# scorer can anchor on, so a backtest reaching back to 2016 scored its opening
# years on technicals alone while the coverage check saw filings and passed.
_IFRS_ACCOUNTS: dict[str, str] = {
    "Revenue": "Revenues",
    "ProfitLossAttributableToOwnersOfParent": "NetIncomeLoss",
    "BasicEarningsLossPerShare": "EarningsPerShareBasic",
    "DilutedEarningsLossPerShare": "EarningsPerShareDiluted",
    "Assets": "Assets",
    "Liabilities": "Liabilities",
    "EquityAttributableToOwnersOfParent": "StockholdersEquity",
    "CashAndCashEquivalents": "CashAndCashEquivalentsAtCarryingValue",
}

# Deliberately absent: `ifrs-full_ProfitLoss` and `ifrs-full_Equity`.
#
# They are the including-noncontrolling-interests totals, and the us-gaap names
# this collector maps onto are not. `NetIncomeLoss` in us-gaap is income
# attributable to the parent — the including-NCI figure is `ProfitLoss`, a
# different element — and `StockholdersEquity` is likewise parent-only against
# `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`.
# The SEC collector pulls those us-gaap names straight from companyfacts, so a
# Korean row filled from the IFRS totals and an American row filled from the
# us-gaap element would sit in the same column meaning different things.
#
# The earlier mapping paired them by name and the gap is not cosmetic. On
# FY2023 filings the two profit figures differ by 34.8% for LG화학, 8.0% for
# POSCO홀딩스 and 6.5% for 삼성전자, and NAVER's parent figure is the larger of
# the two. A cross-sectional comparison between a Korean and a US instrument
# was measuring two different quantities against each other.
#
# Dropping rather than remapping costs coverage where a filer tags only the
# total — 현대자동차 tags only the parent split in 2019 and NAVER only the
# total — and that is the right trade. A year without an anchorable figure is
# reported by the concept count above and refused by the backtest's coverage
# gate; a year holding the wrong quantity under the right name is reported by
# nothing.

# Namespaces one concept may arrive under, newest first.
IFRS_PREFIXES = ("ifrs-full_", "ifrs_")

ACCOUNT_MAP: dict[str, str] = {
    "dart_OperatingIncomeLoss": "OperatingIncomeLoss",
    **{
        f"{prefix}{local}": concept
        for local, concept in _IFRS_ACCOUNTS.items()
        for prefix in IFRS_PREFIXES
    },
}

# What one annual report should yield. A year returning far fewer than this has
# usually met a naming change rather than a company that reports less, which is
# the failure above and is invisible unless counted.
CONCEPTS_PER_REPORT = len(_IFRS_ACCOUNTS) + 1

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


# `사업보고서 (2025.12)`, `[기재정정]반기보고서 (2026.06)`. The period is stated
# only inside the report's name, to month granularity.
_REPORT_PERIOD = re.compile(r"\((\d{4})\.(\d{2})\)")


def report_period_end(report_nm: str) -> date | None:
    """The fiscal period a periodic report covers, from its published name.

    DART states this nowhere else. `list.json` has no period field at all, so
    without parsing the name every filing lands in the register with a null
    period and matches no fiscal period ever — which does not merely lose
    information, it inverts the meaning of the register. A missing match is
    what licenses `NOT_YET_FILED`, so a register that can never match turns
    "the report exists but our value source did not tag this account" into
    "the company has not filed yet".

    Returns the last day of the stated month, which is where every Korean
    periodic report ends, or None when the name carries no period.
    """
    match = _REPORT_PERIOD.search(report_nm)
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return date(year, month, calendar.monthrange(year, month)[1])


def filed_date_from_receipt(rcept_no: str) -> date | None:
    """DART receipt numbers begin with the filing date as YYYYMMDD."""
    if len(rcept_no) < 8 or not rcept_no[:8].isdigit():
        return None
    try:
        return date(int(rcept_no[:4]), int(rcept_no[4:6]), int(rcept_no[6:8]))
    except ValueError:
        return None


# As far back as asking is worth it. DART's full-statement endpoint is indexed
# by business year and returns nothing for years before XBRL filing was in
# place, so a larger number costs empty requests rather than finding more.
MAX_YEARS_BACK = 15


class DartFundamentalCollector(BaseCollector):
    """Fetch Korean annual statements and store every reported revision.

    `years_back` is the whole reach of a collection, and the default is short
    on purpose: a weekly refresh does not need to re-walk a decade. A backtest
    does, and the two are easy to leave disagreeing — ten years of prices
    against five of filings scores the earlier half on technicals alone and
    reports it as the same rule. `python -m app.cli collect --source dart
    --period 10y` is how the longer reach is asked for.
    """

    name = "DART_FUNDAMENTAL"

    def __init__(self, *, years_back: int = 5, guard: QuotaGuard | None = None) -> None:
        settings = get_settings()
        self._key = settings.dart_api_key
        # DART publishes no per-second limit, only a daily quota. The bucket
        # shapes the rate; the guard is what holds the daily total under the
        # published cap.
        self._bucket = TokenBucket(2.0)
        self._guard = guard if guard is not None else QuotaGuard()
        self.years_back = years_back

    def is_configured(self) -> bool:
        return bool(self._key)

    def skip_reason(self) -> str:
        return "set DART_API_KEY to enable (free from opendart.fss.or.kr)"

    # --- transport --------------------------------------------------------

    def _get(self, client: httpx.Client, path: str, **params: str) -> dict[str, Any]:
        # Reserved before it is sent, like every other metered call. Without
        # this the ledger is not the floor `app/core/quota.py` says it is: this
        # collector can spend the whole DART allowance while `quota` reports
        # nothing used, and the next master load then dies on a refusal whose
        # message blames the ledger for being wrong. It is, and this was why.
        self._guard.reserve(QUOTA_GROUP, path.split(".")[0])
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
            decoded = response.json()
        except ValueError as exc:
            raise UpstreamUnavailableError(f"DART returned non-JSON for {path}") from exc

        payload = as_object(decoded, source=f"DART {path}")
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
            # Tracked only. The listing master adds some 3,950 Korean names
            # with neither prices nor a reason to read their filings, and at
            # roughly six calls each that sweep is about 24,000 — over the
            # DART budget and over the published cap behind it.
            for i in instrument_repo.list_active(session, asof=today, tracked=True)
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
                    items = self._accounts(client, corp_code=corp_code, year=year)
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

                    # A year that recognises almost nothing is the signature of
                    # a taxonomy rename, not of a company reporting less. It
                    # looks like a successful collection from every angle
                    # except this count, which is why the count exists: the
                    # `ifrs` to `ifrs-full` change cost four years of Korean
                    # fundamentals and announced itself nowhere.
                    found = {r.concept for r in rows}
                    if len(found) < CONCEPTS_PER_REPORT:
                        missing = sorted(set(ACCOUNT_MAP.values()) - found)
                        warnings.append(
                            f"{instrument.name} {year}: recognised {len(found)} of "
                            f"{CONCEPTS_PER_REPORT} concepts, missing {', '.join(missing)}"
                        )

        session.commit()
        return CollectionResult(
            items_read=read,
            items_saved=saved,
            partial=bool(warnings),
            warnings=warnings,
            detail=f"{len(instruments)} instruments, {self.years_back} years each",
        )

    def _accounts(self, client: httpx.Client, *, corp_code: str, year: int) -> list[Any]:
        """One year of account rows for one company.

        Its own method for the same reason `_collect_filings` is: the shape
        guard on the response is only worth having if a test can reach it, and
        inline inside `collect` the only way in was a database, a tracked
        instrument and a full sweep.
        """
        payload = self._get(
            client,
            "fnlttSinglAcntAll.json",
            corp_code=corp_code,
            bsns_year=str(year),
            reprt_code=ANNUAL_REPORT,
            fs_div="CFS",
        )
        return as_rows(payload.get("list"), source="DART fnlttSinglAcntAll.json")

    def _collect_filings(
        self,
        client: httpx.Client,
        corp_code: str,
        instrument_id: int,
        calendar: MarketCalendar,
    ) -> list[FilingRow]:
        """Periodic disclosures, so absence can be told from non-publication.

        Paged to the end. `list.json` caps a page at 100 and reports how many
        pages there are, and a register that stops at the first page is not
        merely incomplete — the filings it drops are the oldest ones, so the
        register appears to begin later than it does and declines to speak
        about periods it could have witnessed.
        """
        rows: list[FilingRow] = []
        page_no = 1

        while True:
            payload = self._get(
                client,
                "list.json",
                corp_code=corp_code,
                bgn_de="19990101",
                end_de=utc_now().date().strftime("%Y%m%d"),
                pblntf_ty="A",  # 정기공시
                page_count="100",
                page_no=str(page_no),
            )
            items = as_rows(payload.get("list"), source="DART list.json")
            if not items:
                break

            for item in items:
                rcept_no = as_text(item, "rcept_no")
                filed_at = filed_date_from_receipt(rcept_no)
                if filed_at is None:
                    continue
                report_nm = str(item.get("report_nm") or "").strip()
                rows.append(
                    FilingRow(
                        instrument_id=instrument_id,
                        form=report_nm,
                        filed_at=filed_at,
                        period_of_report=report_period_end(report_nm),
                        available_at=calendar.next_session_open(filed_at),
                        accession=rcept_no,
                        source=FundamentalSource.DART,
                    )
                )

            try:
                total_pages = int(payload.get("total_page") or 1)
            except (TypeError, ValueError):
                break
            if page_no >= total_pages:
                break
            page_no += 1

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
            concept = ACCOUNT_MAP.get(as_text(item, "account_id"))
            if concept is None:
                continue

            rcept_no = as_text(item, "rcept_no")
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
