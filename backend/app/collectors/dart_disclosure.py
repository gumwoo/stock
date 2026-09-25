"""Event disclosures from DART for every listed Korean company, a few calls a run.

`list.json` asked without a company returns every filer's disclosures in a
date range, a hundred to a page. So the whole listing master costs a few
dozen calls a run instead of one per company: the two kinds that report
events — 주요사항보고 (B) and 거래소공시 (I) — over the last few days, paged
to the end, kept only for companies in the master.

The transport is the fundamental collector's: the same key, the same quota
group reserved before each call, the same reading of DART's status codes.

Runs before the market opens, with the morning news sweep, so a disclosure
filed the evening before is on record before the close that can use it; and
again in the evening loop.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy.orm import Session

from app.collectors.base import CollectionResult, as_rows, as_text
from app.collectors.dart_fundamental import DartFundamentalCollector, filed_date_from_receipt
from app.collectors.quota import QuotaGuard
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.repositories import disclosure_repo, instrument_repo
from app.repositories.disclosure_repo import DisclosureRow

# 주요사항보고 and 거래소공시: the kinds that announce events.
EVENT_KINDS = ("B", "I")
DEFAULT_DAYS_BACK = 3


class DartDisclosureCollector(DartFundamentalCollector):
    """Event disclosures for the whole Korean master."""

    name = "DART_DISCLOSURE"

    def __init__(self, *, days_back: int = DEFAULT_DAYS_BACK, guard: QuotaGuard | None = None):
        super().__init__(years_back=1, guard=guard)
        self.days_back = days_back

    def collect(self, session: Session) -> CollectionResult:
        calendar = MarketCalendar(Market.KR)
        today = calendar.local_today(utc_now())
        start = today - timedelta(days=self.days_back)
        by_corp = {
            i.kr_corp_code: i.instrument_id
            for i in instrument_repo.list_active(
                session, asof=today, market=Market.KR, tracked=None
            )
            if i.kr_corp_code
        }

        rows: list[DisclosureRow] = []
        read = foreign = calls = 0
        with httpx.Client() as client:
            for kind in EVENT_KINDS:
                page = 1
                while True:
                    payload = self._get(
                        client,
                        "list.json",
                        bgn_de=start.strftime("%Y%m%d"),
                        end_de=today.strftime("%Y%m%d"),
                        pblntf_ty=kind,
                        page_count="100",
                        page_no=str(page),
                    )
                    calls += 1
                    items = as_rows(payload.get("list"), source="DART list.json")
                    for item in items:
                        read += 1
                        instrument_id = by_corp.get(as_text(item, "corp_code") or "")
                        if instrument_id is None:
                            foreign += 1
                            continue
                        rcept_no = as_text(item, "rcept_no") or ""
                        filed_on = filed_date_from_receipt(rcept_no)
                        report_nm = as_text(item, "report_nm")
                        if filed_on is None or not report_nm or not calendar.covers(filed_on):
                            continue
                        rows.append(
                            DisclosureRow(
                                instrument_id=instrument_id,
                                rcept_no=rcept_no,
                                report_nm=report_nm[:300],
                                pblntf_ty=kind,
                                filer=(as_text(item, "flr_nm") or None),
                                filed_on=filed_on,
                                available_at=calendar.next_session_open(filed_on),
                            )
                        )
                    try:
                        pages = int(str(payload.get("total_page") or 1))
                    except ValueError:
                        pages = 1
                    if not items or page >= pages:
                        break
                    page += 1

        saved = disclosure_repo.save_disclosures(session, rows)
        session.commit()
        return CollectionResult(
            items_read=read,
            items_saved=saved,
            detail=(
                f"{start}..{today}, kinds {','.join(EVENT_KINDS)}: {read} read in {calls} calls, "
                f"{len(rows)} for listed companies, {foreign} for others, {saved} new"
            ),
        )
