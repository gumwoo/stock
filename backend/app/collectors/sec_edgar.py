"""SEC EDGAR XBRL facts.

The primary source for US fundamentals, and the reason the whole point-in-time
design is possible on that side of the portfolio. Every fact in companyfacts
carries its own `filed` date and accession number, and the same fiscal period
appears repeatedly as later filings restate it. That is what makes "the figure
as known on date X" recoverable rather than a guess.

yfinance, by contrast, exposes only the latest restated numbers with no filing
date, so it can fill gaps in the forward window but must never be used to
reconstruct a past view.

**Access notes, learned the hard way.**

No API key is required, but SEC demands a User-Agent identifying the caller
with a contact address, and its WAF rejects some email domains outright with
"Undeclared Automated Tool" regardless of headers, HTTP version or TLS stack.
See `.env.example`. Sending a browser User-Agent does get through, and is not
done here: SEC blocks undeclared automation deliberately, and pretending to be
Chrome would misrepresent the client to a government service.

The published limit is 10 requests per second, and exceeding it blocks the IP
for around ten minutes, so the bucket is set to 8.
"""

from __future__ import annotations

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
from app.repositories import fundamental_repo, instrument_repo
from app.repositories.fundamental_repo import FundamentalRow

logger = logging.getLogger(__name__)

BASE = "https://data.sec.gov"

# The concepts the fundamental engine needs. Deliberately a short list: pulling
# every concept a filer tags would be tens of thousands of rows per company,
# most of them never read.
WANTED_CONCEPTS: dict[str, tuple[str, ...]] = {
    "us-gaap": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "NetIncomeLoss",
        "EarningsPerShareBasic",
        "EarningsPerShareDiluted",
        "Assets",
        "Liabilities",
        "StockholdersEquity",
        "OperatingIncomeLoss",
        "CashAndCashEquivalentsAtCarryingValue",
    ),
}

# Periodic reports **and their amendments**. Excluding the "/A" forms looks
# tidy and is wrong: an amendment is often exactly where a restatement first
# becomes public. Apple restated FY2008 basic EPS from 5.48 to 6.94 in a 10-K/A
# filed 2010-01-25; dropping that form pushed the restatement's apparent
# publication date out to the next annual 10-K on 2010-10-27, nine months late.
# For a system whose whole claim is knowing what the market knew when, that is
# the worst kind of error — quiet, and in the direction of confidence.
#
# 8-K is still excluded: it carries real numbers but irregularly, and mixing it
# into a periodic series makes period-over-period comparison meaningless.
WANTED_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
    }
)


def _fiscal_period(fp: str | None) -> FiscalPeriod:
    if not fp:
        return FiscalPeriod.UNKNOWN
    try:
        return FiscalPeriod(fp.upper())
    except ValueError:
        return FiscalPeriod.UNKNOWN


class SecEdgarCollector(BaseCollector):
    """Fetch XBRL company facts and store every filed revision."""

    name = "SEC_EDGAR"

    def __init__(self, *, concepts: dict[str, tuple[str, ...]] | None = None) -> None:
        settings = get_settings()
        self._user_agent = settings.sec_user_agent
        self._bucket = TokenBucket(settings.sec_rate)
        self.concepts = concepts or WANTED_CONCEPTS

    def is_configured(self) -> bool:
        return bool(self._user_agent.strip())

    def skip_reason(self) -> str:
        return (
            "set SEC_USER_AGENT to enable (no API key needed — just "
            "'<app name> <your email>'; note SEC rejects some email domains)"
        )

    # --- transport --------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json",
        }

    def _get(self, client: httpx.Client, path: str) -> dict[str, Any]:
        """One rate-limited request, with SEC's failure modes made typed."""
        self._bucket.acquire()
        try:
            response = client.get(f"{BASE}{path}", headers=self._headers(), timeout=60)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"SEC request failed for {path}: {exc}") from exc

        if response.status_code == 429:
            raise RateLimitedError(f"SEC rate limited on {path}")
        if response.status_code == 403:
            raise UpstreamUnavailableError(
                f"SEC returned 403 for {path}. The User-Agent was rejected — SEC "
                "refuses some email domains outright. See .env.example."
            )
        if response.status_code == 404:
            raise UpstreamUnavailableError(f"SEC has no data at {path}")
        if response.status_code != 200:
            raise UpstreamUnavailableError(f"SEC returned {response.status_code} for {path}")

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise UpstreamUnavailableError(f"SEC returned non-JSON for {path}") from exc
        return payload

    # --- collection -------------------------------------------------------

    def collect(self, session: Session) -> CollectionResult:
        instruments = [
            i for i in instrument_repo.list_active(session, asof=utc_now().date()) if i.us_cik
        ]
        if not instruments:
            return CollectionResult(detail="no US instruments with a CIK")

        calendar = MarketCalendar(Market.US)
        read = saved = 0
        warnings: list[str] = []

        with httpx.Client() as client:
            for instrument in instruments:
                cik = str(instrument.us_cik).zfill(10)
                facts = self._get(client, f"/api/xbrl/companyfacts/CIK{cik}.json")

                rows, seen, skipped = self._to_rows(facts, instrument.instrument_id, calendar)
                read += seen
                saved += fundamental_repo.save_facts(session, rows)
                if skipped:
                    warnings.append(f"CIK {cik}: {skipped} facts skipped (unparseable or off-form)")

        session.commit()
        return CollectionResult(
            items_read=read,
            items_saved=saved,
            partial=bool(warnings),
            warnings=warnings,
            detail=f"{len(instruments)} instruments, {sum(len(c) for c in self.concepts.values())} concepts",
        )

    def _to_rows(
        self,
        payload: dict[str, Any],
        instrument_id: int,
        calendar: MarketCalendar,
    ) -> tuple[list[FundamentalRow], int, int]:
        """Flatten companyfacts into rows, preserving every filed revision.

        The nesting is `facts[taxonomy][concept]["units"][unit] -> [facts]`, and
        the unit key matters: dropping it would let a USD revenue and a
        USD/shares EPS land in the same series.
        """
        rows: list[FundamentalRow] = []
        seen = skipped = 0

        facts = payload.get("facts", {})
        for taxonomy, concepts in self.concepts.items():
            available = facts.get(taxonomy, {})
            for concept in concepts:
                entry = available.get(concept)
                if not entry:
                    continue

                for unit, items in entry.get("units", {}).items():
                    for item in items:
                        seen += 1
                        row = self._to_row(
                            item,
                            instrument_id=instrument_id,
                            taxonomy=taxonomy,
                            concept=concept,
                            unit=unit,
                            calendar=calendar,
                        )
                        if row is None:
                            skipped += 1
                        else:
                            rows.append(row)

        return rows, seen, skipped

    @staticmethod
    def _to_row(
        item: dict[str, Any],
        *,
        instrument_id: int,
        taxonomy: str,
        concept: str,
        unit: str,
        calendar: MarketCalendar,
    ) -> FundamentalRow | None:
        form = item.get("form")
        if form not in WANTED_FORMS:
            return None

        filed = item.get("filed")
        end = item.get("end")
        value = item.get("val")
        if not filed or not end or value is None:
            return None

        try:
            filed_at = date.fromisoformat(filed)
            period_end = date.fromisoformat(end)
            period_start = date.fromisoformat(item["start"]) if item.get("start") else None
            amount = Decimal(str(value))
        except (ValueError, InvalidOperation):
            return None

        return FundamentalRow(
            instrument_id=instrument_id,
            taxonomy=taxonomy,
            concept=concept,
            unit=unit,
            period_start=period_start,
            period_end=period_end,
            fiscal_year=item.get("fy"),
            fiscal_period=_fiscal_period(item.get("fp")),
            form=str(form),
            value=amount,
            filed_at=filed_at,
            # A filing date has no time of day, so the honest boundary is the
            # next session's open rather than the filing date itself.
            available_at=calendar.next_session_open(filed_at),
            accession=item.get("accn"),
            source=FundamentalSource.SEC,
            frame=item.get("frame"),
        )
