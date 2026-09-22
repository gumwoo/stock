"""The Korean listing master: every company a news article might be about.

Names only. No prices, no filings, no scoring — those cost per company and are
worth paying for once a name is worth following, not before. What this buys is
the ability to read "테스트반도체가 인수" and know which instrument that is.

**`corpCode.xml` cannot say which exchange.** It carries `corp_code`,
`corp_name`, `corp_eng_name`, `stock_code` and `modify_date`, and a present
`stock_code` means only "listed somewhere". The board is in `corp_cls`:
`Y` for KOSPI, `K` for KOSDAQ, `N` for KONEX, `E` for everything else. So the
file gives candidates and a second source gives the market.

**That second source is the filing list, not the company profile.** Asking
`company.json` once per candidate is roughly 2,500 calls. `list.json` answers
without a `corp_code` and returns `corp_cls` on every row, so sweeping a year
of periodic filings harvests the same mapping for most of the market in about
150. Measured: one quarter of `pblntf_ty=A` is 3,600 filings over 36 pages.
The range is capped at three months when no `corp_code` is given — DART says so
outright, status 100 — so a year is four sweeps rather than one.

Whatever the sweep misses falls back to `company.json`, one call each, under
the same budget. Companies that filed nothing all year are mostly ones we could
not score anyway.

**Only `Y` and `K` are stored.** KONEX and the rest are outside what this is
for, and `Listing` has no room for them. Writing every candidate first and
filtering later would leave rows to clean up.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
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
from app.collectors.quota import QuotaExhausted, QuotaGuard
from app.config import get_settings
from app.core.calendar import Market
from app.core.clock import utc_now
from app.models.instrument import Listing
from app.repositories import instrument_repo

BASE = "https://opendart.fss.or.kr/api"

QUOTA_GROUP = "dart"

# DART refuses a range wider than this when no corp_code is given.
MAX_RANGE = timedelta(days=90)
PAGE_SIZE = 100

# `corp_cls` to the board it names. KONEX (`N`) and other (`E`) are not stored:
# this exists to cover KOSPI and KOSDAQ, and `Listing` says so.
BOARDS: Mapping[str, Listing] = {"Y": Listing.KOSPI, "K": Listing.KOSDAQ}

# A ZIP begins with one of these. `corpCode.xml` answers with an archive when
# it works and with an XML document when it does not, at HTTP 200 either way,
# so the first four bytes are what tells them apart.
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# DART's published status codes. Kept here so a failure names what happened
# instead of quoting a bare number at whoever reads the run afterwards.
DART_STATUS: Mapping[str, str] = {
    "010": "등록되지 않은 키",
    "011": "사용할 수 없는 키",
    "012": "접근할 수 없는 IP",
    "013": "조회된 데이터 없음",
    "014": "파일이 존재하지 않음",
    "020": "요청 제한 초과",
    "021": "조회 가능한 회사 개수 초과",
    "100": "필드의 부적절한 값",
    "101": "부적절한 접근",
    "800": "시스템 점검",
    "900": "정의되지 않은 오류",
    "901": "오픈API 이용동의 필요",
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """A listed company from `corpCode.xml`, before its board is known."""

    corp_code: str
    name: str
    stock_code: str


class KrxMasterCollector(BaseCollector):
    """Loads Korean listed names, with the board each one trades on."""

    name = "KRX_MASTER"

    def __init__(
        self,
        *,
        years_back: int = 1,
        guard: QuotaGuard | None = None,
        fill_gaps: bool = True,
    ) -> None:
        settings = get_settings()
        self._key = settings.dart_api_key
        self._bucket = TokenBucket(2.0)
        self._guard = guard if guard is not None else QuotaGuard()
        self.years_back = years_back
        # Whether to spend a call each on companies the filing sweep missed.
        # Off makes the run cheap and the master slightly thinner.
        self.fill_gaps = fill_gaps

    def is_configured(self) -> bool:
        return bool(self._key)

    def skip_reason(self) -> str:
        return "set DART_API_KEY to enable (free from opendart.fss.or.kr)"

    # --- pure conversion --------------------------------------------------

    @staticmethod
    def check_archive(payload: bytes) -> None:
        """Refuse an error document dressed up as an archive.

        `corpCode.xml` returns a ZIP when it works and an XML `<result>` when
        it does not — a key that is not registered, a quota refusal, a
        maintenance window — and it answers HTTP 200 for all of them. Handed
        straight to `zipfile`, every one of those becomes `BadZipFile`, which
        is not a `CollectorError`; `run_collector` then re-raises it and files
        the run as "internal error". An outage would be indistinguishable from
        a bug in this file, and telling those apart is the one thing the error
        taxonomy exists to do.

        The sibling endpoints already classify status `020`. This one did not,
        and it is the first call every run makes.
        """
        if payload[:4] in ZIP_MAGIC:
            return

        status: str | None = None
        message: str | None = None
        try:
            root = ET.fromstring(payload.decode("utf-8", errors="replace"))
        except ET.ParseError:
            root = None
        if root is not None:
            status = (root.findtext("status") or "").strip() or None
            message = (root.findtext("message") or "").strip() or None

        if status == "020":
            # Their counter and ours disagree, which makes our accounting
            # wrong. Loud, for the same reason it is loud in `_get_json`.
            raise RateLimitedError(
                "DART refused corpCode.xml as over quota, but our ledger had room. "
                "The ledger is wrong; check the budget before collecting again"
            )
        if status is not None:
            raise UpstreamUnavailableError(
                f"DART corpCode.xml returned status {status} "
                f"({DART_STATUS.get(status, 'undocumented')}): {message}"
            )
        raise UpstreamUnavailableError(
            "DART corpCode.xml returned neither an archive nor a status document "
            f"({len(payload)} bytes beginning {payload[:16]!r})"
        )

    @classmethod
    def parse_corp_codes(cls, payload: bytes) -> list[Candidate]:
        """Listed companies out of the `corpCode.xml` archive.

        A blank `stock_code` is an unlisted company, of which DART has far more
        than listed ones. `.strip()` matters: the field is space-padded rather
        than empty for many rows.
        """
        cls.check_archive(payload)

        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            name = next(n for n in archive.namelist() if n.lower().endswith(".xml"))
            root = ET.fromstring(archive.read(name).decode("utf-8"))

        found: list[Candidate] = []
        for node in root.iter("list"):
            stock_code = (node.findtext("stock_code") or "").strip()
            corp_code = (node.findtext("corp_code") or "").strip()
            corp_name = (node.findtext("corp_name") or "").strip()
            if stock_code and corp_code and corp_name:
                found.append(Candidate(corp_code=corp_code, name=corp_name, stock_code=stock_code))
        return found

    @staticmethod
    def quarters(*, end: date, years_back: int) -> Iterator[tuple[date, date]]:
        """Ranges no wider than DART accepts, newest first.

        Newest first so that a budget running out costs the oldest quarter,
        which is the one least likely to hold a listing we do not already have.
        """
        cursor = end
        floor = end - timedelta(days=365 * years_back)
        while cursor > floor:
            start = max(floor, cursor - MAX_RANGE + timedelta(days=1))
            yield start, cursor
            cursor = start - timedelta(days=1)

    # --- transport --------------------------------------------------------

    def _reserve(self, endpoint: str) -> None:
        self._guard.reserve(QUOTA_GROUP, endpoint)
        self._bucket.acquire()

    def _get_json(self, client: httpx.Client, path: str, **params: str) -> dict[str, Any]:
        self._reserve(path.split(".")[0])
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
            raise RateLimitedError(
                "DART refused the request as over quota, but our ledger had room. "
                "The ledger is wrong; check the budget before collecting again"
            )
        if status == "013":
            # No rows for this range. A normal answer, not a fault.
            return {"status": status, "list": [], "total_page": 0}
        if status != "000":
            raise UpstreamUnavailableError(
                f"DART {path} returned status {status}: {payload.get('message')}"
            )
        return payload

    def _corp_code_archive(self, client: httpx.Client) -> bytes:
        self._reserve("corpCode")
        try:
            response = client.get(
                f"{BASE}/corpCode.xml", params={"crtfc_key": self._key}, timeout=120
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"DART corpCode.xml failed: {exc}") from exc
        if response.status_code != 200:
            raise UpstreamUnavailableError(f"DART returned {response.status_code} for corpCode.xml")
        return response.content

    # --- collection -------------------------------------------------------

    def boards_from_filings(
        self, client: httpx.Client, *, end: date
    ) -> tuple[dict[str, str], str | None]:
        """Sweep periodic filings for `corp_cls`, newest quarter first.

        Returns the mapping and, if the budget ran out, the reason. A partial
        mapping is useful on its own: every company it did cover is one fewer
        profile call to make.
        """
        boards: dict[str, str] = {}
        for start, finish in self.quarters(end=end, years_back=self.years_back):
            page = 1
            while True:
                try:
                    payload = self._get_json(
                        client,
                        "list.json",
                        bgn_de=start.strftime("%Y%m%d"),
                        end_de=finish.strftime("%Y%m%d"),
                        pblntf_ty="A",
                        page_no=str(page),
                        page_count=str(PAGE_SIZE),
                    )
                except QuotaExhausted as refused:
                    return boards, str(refused)

                for row in payload.get("list") or []:
                    corp_code = (row.get("corp_code") or "").strip()
                    corp_cls = (row.get("corp_cls") or "").strip()
                    if corp_code and corp_cls:
                        boards.setdefault(corp_code, corp_cls)

                total = int(payload.get("total_page") or 0)
                if page >= total:
                    break
                page += 1
        return boards, None

    def collect(self, session: Session) -> CollectionResult:
        today = utc_now().date()
        warnings: list[str] = []

        with httpx.Client() as client:
            # A refusal here is left to propagate: nothing has been collected
            # yet, so SKIPPED is the honest status and `QuotaExhausted` already
            # carries the reason.
            archive = self._corp_code_archive(client)

            candidates = self.parse_corp_codes(archive)
            if not candidates:
                raise UpstreamUnavailableError("corpCode.xml held no listed companies")

            boards, stopped = self.boards_from_filings(client, end=today)
            if stopped:
                warnings.append(f"filing sweep stopped: {stopped}")

            missing = [c for c in candidates if c.corp_code not in boards]
            profiled = 0
            if self.fill_gaps and not stopped:
                for candidate in missing:
                    try:
                        payload = self._get_json(
                            client, "company.json", corp_code=candidate.corp_code
                        )
                    except QuotaExhausted as refused:
                        warnings.append(f"profile lookup stopped: {refused}")
                        break
                    corp_cls = (payload.get("corp_cls") or "").strip()
                    if corp_cls:
                        boards[candidate.corp_code] = corp_cls
                    profiled += 1

        written = 0
        skipped_board = 0
        for candidate in candidates:
            listing = BOARDS.get(boards.get(candidate.corp_code, ""))
            if listing is None:
                # Unknown, or KONEX and friends. Neither is stored: a row with
                # no board cannot be given a price-source suffix later, and
                # writing it now would leave something to clean up.
                skipped_board += 1
                continue
            instrument = instrument_repo.upsert_instrument(
                session,
                market=Market.KR,
                name=candidate.name,
                symbol=candidate.stock_code,
                kr_corp_code=candidate.corp_code,
                symbol_source="MASTER",
            )
            # Set here rather than through the upsert, which is shared with
            # seeding and has no business knowing about boards. `tracked` is
            # deliberately untouched: this run establishes that a company
            # exists, not that anyone follows it.
            instrument.listing = listing
            written += 1

        session.commit()

        return CollectionResult(
            items_read=len(candidates),
            items_saved=written,
            partial=bool(warnings),
            warnings=warnings,
            detail=(
                f"{len(candidates)} listed candidates, {len(boards)} boards resolved "
                f"({profiled} by profile lookup), {written} stored as KOSPI or KOSDAQ, "
                f"{skipped_board} skipped as KONEX, other or unresolved"
            ),
        )
