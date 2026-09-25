"""Daily search trends from Naver DataLab, through NAVER API Hub, for the names in focus.

One company per request: the provider scales each request to its busiest
series, and a small company batched with a large one comes back as rounding
(see `app/models/attention.py`). The names are the tracked ones and the recent
candidates, which the caller passes; the whole master would be two thousand
calls a day for names nothing reads.

The keywords are the company name with 주가 and 주식, which are searches about
the stock. The bare name would also count people looking for a garden (원림)
or for menswear (남성), the same trap the news relevance rule exists for.

Every call is reserved on the `naver_datalab` quota before it is sent. The
response is JSON served as `text/plain`, so it is parsed from the text.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from datetime import timedelta
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
from app.collectors.quota import QuotaExhausted, QuotaGuard
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.repositories import attention_repo, instrument_repo

BASE = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
QUOTA_GROUP = "naver_datalab"
ENDPOINT = "search_trend"
DEFAULT_DAYS_BACK = 60


def keywords_for(name: str) -> list[str]:
    return [f"{name} 주가", f"{name}주가", f"{name} 주식"]


def parse_series(payload: Mapping[str, Any]) -> dict[str, float]:
    """The one group's points as ISO day to ratio. Anything malformed is an upstream error."""
    results = as_rows(payload.get("results"), source="DataLab results")
    if len(results) != 1 or not isinstance(results[0], Mapping):
        raise UpstreamUnavailableError(f"DataLab returned {len(results)} groups for one asked")
    series: dict[str, float] = {}
    for point in as_rows(results[0].get("data"), source="DataLab data"):
        if not isinstance(point, Mapping):
            raise UpstreamUnavailableError(f"DataLab point {point!r}"[:200])
        day = as_text(point, "period")
        ratio = point.get("ratio")
        if (
            len(day) != 10
            or isinstance(ratio, bool)
            or not isinstance(ratio, int | float)
            or ratio < 0
        ):
            raise UpstreamUnavailableError(f"DataLab point {dict(point)!r}"[:200])
        series[day] = float(ratio)
    return series


class NaverDataLabCollector(BaseCollector):
    """Search trends for the named instruments, one request each."""

    name = "NAVER_DATALAB"

    def __init__(
        self,
        *,
        instrument_ids: Collection[int],
        days_back: int = DEFAULT_DAYS_BACK,
        guard: QuotaGuard | None = None,
    ) -> None:
        settings = get_settings()
        self._client_id = settings.naver_client_id
        self._client_secret = settings.naver_client_secret
        self._bucket = TokenBucket(settings.naver_rate)
        self._guard = guard if guard is not None else QuotaGuard()
        self.instrument_ids = frozenset(instrument_ids)
        self.days_back = days_back

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def skip_reason(self) -> str:
        return (
            "set NAVER_CLIENT_ID and NAVER_CLIENT_SECRET, with Search Trend enabled "
            "on the NAVER API Hub application"
        )

    def collect(self, session: Session) -> CollectionResult:
        calendar = MarketCalendar(Market.KR)
        # Up to yesterday: today's searches are not over.
        end = calendar.local_today(utc_now()) - timedelta(days=1)
        start = end - timedelta(days=self.days_back)
        names = [
            i
            for i in instrument_repo.list_active(session, asof=end, market=Market.KR, tracked=None)
            if i.instrument_id in self.instrument_ids
        ]
        stored = empty = 0
        partial = False
        warnings: list[str] = []
        with httpx.Client() as client:
            for instrument in names:
                words = keywords_for(instrument.name)
                try:
                    payload = self._post(
                        client,
                        {
                            "startDate": start.isoformat(),
                            "endDate": end.isoformat(),
                            "timeUnit": "date",
                            "keywordGroups": [{"groupName": instrument.name, "keywords": words}],
                        },
                    )
                except QuotaExhausted as refused:
                    if stored == 0:
                        # Refused before anything: a skipped run, not a partial one.
                        raise
                    partial = True
                    warnings.append(f"stopped by the quota after {stored} names: {refused}")
                    break
                except UpstreamUnavailableError as exc:
                    # One name's bad answer is that name's gap, not the run's.
                    partial = True
                    warnings.append(f"{instrument.name}: {exc}")
                    continue
                try:
                    series = parse_series(payload)
                except UpstreamUnavailableError as exc:
                    partial = True
                    warnings.append(f"{instrument.name}: {exc}")
                    continue
                attention_repo.save_trend(
                    session,
                    instrument_id=instrument.instrument_id,
                    start_date=start,
                    end_date=end,
                    keywords=words,
                    series=series,
                )
                stored += 1
                empty += not series
        session.commit()
        return CollectionResult(
            items_read=len(names),
            items_saved=stored,
            partial=partial,
            warnings=warnings,
            detail=f"{start}..{end}: {stored} of {len(names)} names, {empty} too little searched",
        )

    def _post(self, client: httpx.Client, body: dict[str, Any]) -> dict[str, Any]:
        """One trend request, reserved before it is sent."""
        self._guard.reserve(QUOTA_GROUP, ENDPOINT)
        self._bucket.acquire()
        try:
            response = client.post(
                BASE,
                content=json.dumps(body),
                headers={
                    "X-NCP-APIGW-API-KEY-ID": self._client_id,
                    "X-NCP-APIGW-API-KEY": self._client_secret,
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"DataLab request failed: {exc}") from exc
        if response.status_code == 429:
            # Our ledger had room and theirs did not: an accounting bug, loudly.
            raise RateLimitedError(
                "DataLab refused the request as over quota, but our ledger had room. "
                "The ledger is wrong; check the budget before collecting again"
            )
        if response.status_code != 200:
            raise UpstreamUnavailableError(
                f"DataLab returned {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            raise UpstreamUnavailableError("DataLab returned non-JSON") from exc
        return as_object(payload, source="DataLab search trend")
