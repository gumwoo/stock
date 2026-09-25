"""One-minute bars from KIS: each stock's day after the close, and the indexes during the session.

**A stock's day** is fetched with the past-minute-bar call, which returns up to
120 bars ending at a given time. It is walked back from 15:30: each next page
starts at the oldest bar seen, which the provider returns again, so bars are
keyed by time and a repeat is dropped. The day is COMPLETE when a page reaches
09:00 or runs into the day before (past 09:00 the provider carries on into the
previous day's after-hours bars, which are not kept). A walk that stops short —
the page cap, a cursor that does not move — is PARTIAL, and that day is not
analysed. Only 09:00 to 15:30 is kept.

**The indexes** cannot be fetched for a past day at all: the call takes no
date and returns only the latest session's last hundred or so minutes. So they
are asked for during the session, often enough that the pieces overlap, and a
day is whole only if all 391 minutes from 09:00 to 15:30 arrived. The rows
`888888` and `999999` are the provider's after-close and final-close lines,
not minutes, and are not kept.

Every call goes through `KisClient`, which reserves it on the quota first.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.collectors.base import (
    BaseCollector,
    CollectionResult,
    SkipCollection,
    UpstreamUnavailableError,
    as_rows,
    as_text,
)
from app.collectors.kis import KisClient
from app.collectors.quota import QuotaExhausted
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.db import advisory_lock, session_scope
from app.repositories import instrument_repo, minute_repo
from app.repositories.minute_repo import (
    COMPLETE,
    EMPTY,
    ERROR,
    PARTIAL,
    SETTLED,
    IndexMinuteRow,
    MinuteBarRow,
)

SEOUL = ZoneInfo("Asia/Seoul")
STOCK_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
STOCK_TR = "FHKST03010230"
INDEX_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-indexchartprice"
INDEX_TR = "FHKUP03500200"
# KIS's index codes, stored under the codes the daily index bars use.
INDEXES = {"0001": "^KS11", "1001": "^KQ11"}

OPEN = "090000"
CLOSE = "153000"
# 390 minutes from 09:00 to 15:29 and the 15:30 closing line.
INDEX_MINUTES = 391
# Four pages cover a full day; the rest is room for a thin day's gaps.
MAX_PAGES = 8
DEFAULT_BACKFILL_SESSIONS = 60
DEFAULT_MAX_CALLS = 3_000
# KIS paces per app key, and each process paces only itself: two KIS runs at
# once, a hand-run backfill beside the worker's, would call at twice the rate.
# So every run that calls KIS holds this lock for its duration.
KIS_RUN_LOCK = "kis_rest_run"


def kis_run_lock() -> Any:
    """Hold the one-KIS-run lock, or skip: another run is calling KIS right now."""
    from contextlib import contextmanager

    @contextmanager
    def held() -> Any:
        # A session of its own, never committed while held, so the lock stays
        # on one connection however often the collector commits.
        with session_scope() as lock_session, advisory_lock(lock_session, KIS_RUN_LOCK) as ok:
            if not ok:
                raise SkipCollection(
                    "another KIS run is in progress; not calling at twice the rate"
                )
            yield

    return held()


def minute_start(day: date, hhmmss: str) -> datetime:
    """The UTC instant a KIS minute label names: the start of that minute in Seoul."""
    clock = time(int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6]))
    return datetime.combine(day, clock, tzinfo=SEOUL).astimezone(UTC)


def _number(row: Mapping[str, Any], key: str) -> Decimal:
    raw = as_text(row, key)
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise UpstreamUnavailableError(f"KIS minute bar {key}={raw!r}") from None
    if not value.is_finite() or value < 0:
        raise UpstreamUnavailableError(f"KIS minute bar {key}={raw!r}")
    return value


def _label(row: Mapping[str, Any], key: str) -> str:
    raw = as_text(row, key)
    if len(raw) != 6 or not raw.isdigit():
        raise UpstreamUnavailableError(f"KIS minute label {key}={raw!r}")
    return raw


def stock_bar(instrument_id: int, day: date, row: Mapping[str, Any]) -> MinuteBarRow:
    start = minute_start(day, _label(row, "stck_cntg_hour"))
    close = _number(row, "stck_prpr")
    if close <= 0:
        raise UpstreamUnavailableError(f"KIS minute bar with price {close}")
    return MinuteBarRow(
        instrument_id=instrument_id,
        session_date=day,
        ts=start,
        available_at=start + timedelta(minutes=1),
        open=_number(row, "stck_oprc"),
        high=_number(row, "stck_hgpr"),
        low=_number(row, "stck_lwpr"),
        close=close,
        volume=_number(row, "cntg_vol"),
    )


def in_session(hhmmss: str) -> bool:
    return OPEN <= hhmmss <= CLOSE


class DayWalk:
    """The pages of one stock's day, as they arrive."""

    def __init__(self, day: date) -> None:
        self.day = day
        self.key = day.strftime("%Y%m%d")
        self.rows: dict[str, Mapping[str, Any]] = {}
        self.pages = 0
        self.cursor = CLOSE
        self.status: str | None = None

    def take(self, page: Sequence[Any]) -> None:
        """Read one page and decide whether the walk is over."""
        self.pages += 1
        same: list[Mapping[str, Any]] = []
        earlier = False
        for row in page:
            if not isinstance(row, Mapping):
                raise UpstreamUnavailableError(f"KIS minute row {row!r}"[:200])
            day = as_text(row, "stck_bsop_date")
            if day == self.key:
                same.append(row)
            elif day and day < self.key:
                earlier = True
        if not same:
            # Only the day before appearing proves the day is over: then it is
            # COMPLETE if it had bars and EMPTY if it had none. A page with
            # nothing at all proves nothing, and the day is asked for again.
            if earlier:
                self.status = COMPLETE if self.rows else EMPTY
            else:
                self.status = PARTIAL
            return
        labels = [_label(r, "stck_cntg_hour") for r in same]
        for label, row in zip(labels, same, strict=True):
            if in_session(label):
                self.rows[label] = row
        oldest = min(labels)
        if earlier or oldest <= OPEN:
            self.status = COMPLETE if self.rows else EMPTY
        elif self.pages > 1 and oldest >= self.cursor:
            # The cursor did not move: asking again would return the same page.
            self.status = PARTIAL
        elif self.pages >= MAX_PAGES:
            self.status = PARTIAL
        else:
            self.cursor = oldest


class KisMinuteCollector(BaseCollector):
    """A stock's minute bars for today (after the close) and, bounded, its recent past."""

    name = "KIS_MINUTE"

    def __init__(
        self,
        *,
        instrument_ids: Collection[int],
        backfill_sessions: int = 0,
        max_calls: int = DEFAULT_MAX_CALLS,
        client: KisClient | None = None,
    ) -> None:
        self.instrument_ids = frozenset(instrument_ids)
        self.backfill_sessions = backfill_sessions
        self.max_calls = max_calls
        self._client = client

    def is_configured(self) -> bool:
        return get_settings().kis_enabled

    def skip_reason(self) -> str:
        return "set KIS_APP_KEY and KIS_APP_SECRET (a KIS Open API app key)"

    def days(self, now: datetime) -> list[date]:
        """Newest first: today if its session is over, then the backfill window before it."""
        calendar = MarketCalendar(Market.KR)
        today = calendar.local_today(now)
        start = today - timedelta(days=self.backfill_sessions * 2 + 14)
        sessions = [d for d in calendar.sessions_between(start, today) if d < today]
        past = list(reversed(sessions))[: self.backfill_sessions]
        head = [today] if calendar.is_session(today) and calendar.has_closed(now) else []
        return head + past

    def collect(self, session: Session) -> CollectionResult:
        with kis_run_lock():
            return self._collect(session)

    def _collect(self, session: Session) -> CollectionResult:
        now = utc_now()
        today = MarketCalendar(Market.KR).local_today(now)
        days = self.days(now)
        names = [
            i
            for i in instrument_repo.list_active(
                session, asof=now.date(), market=Market.KR, tracked=None
            )
            if i.instrument_id in self.instrument_ids
        ]
        settled = minute_repo.latest_status(
            session, instrument_ids=[i.instrument_id for i in names], days=days
        )
        todo = [
            (day, i)
            for day in days
            for i in names
            if settled.get((i.instrument_id, day)) not in SETTLED
        ]
        client = self._client or KisClient()
        counts = {COMPLETE: 0, PARTIAL: 0, EMPTY: 0, ERROR: 0}
        bars = calls = 0
        stopped: str | None = None
        warnings: list[str] = []
        try:
            for day, instrument in todo:
                if calls + MAX_PAGES > self.max_calls:
                    stopped = f"call cap {self.max_calls} reached"
                    break
                symbol = instrument_repo.current_symbol(session, instrument.instrument_id)
                if symbol is None:
                    warnings.append(f"{instrument.name}: no current symbol")
                    continue
                walk = DayWalk(day)
                try:
                    while walk.status is None:
                        body, _ = client.get(
                            STOCK_PATH,
                            tr_id=STOCK_TR,
                            params={
                                "FID_COND_MRKT_DIV_CODE": "J",
                                "FID_INPUT_ISCD": symbol,
                                "FID_INPUT_HOUR_1": walk.cursor,
                                "FID_INPUT_DATE_1": walk.key,
                                "FID_PW_DATA_INCU_YN": "Y",
                                "FID_FAKE_TICK_INCU_YN": "N",
                            },
                        )
                        calls += 1
                        walk.take(as_rows(body.get("output2"), source="KIS minute bars"))
                    rows = [stock_bar(instrument.instrument_id, day, r) for r in walk.rows.values()]
                except QuotaExhausted as refused:
                    stopped = f"quota: {refused}"
                    break
                except UpstreamUnavailableError as exc:
                    # One name's bad day is that day's gap: recorded, asked
                    # again next run, and the rest of the run goes on. A rate
                    # refusal is not this and still stops everything.
                    session.rollback()
                    warnings.append(f"{instrument.name} {day}: {exc}")
                    minute_repo.record_fetch(
                        session,
                        instrument_id=instrument.instrument_id,
                        day=day,
                        status=ERROR,
                        bars=0,
                        pages=walk.pages,
                    )
                    session.commit()
                    counts[ERROR] += 1
                    continue
                assert walk.status is not None
                if walk.status == EMPTY and day >= today:
                    # Today's bars may not be served yet; an empty today is not settled.
                    walk.status = PARTIAL
                bars += minute_repo.save_bars(session, rows)
                minute_repo.record_fetch(
                    session,
                    instrument_id=instrument.instrument_id,
                    day=day,
                    status=walk.status,
                    bars=len(rows),
                    pages=walk.pages,
                )
                # Per day, so a stop later keeps what came in.
                session.commit()
                counts[walk.status] += 1
        finally:
            if self._client is None:
                client.close()
        if stopped:
            warnings.append(stopped)
        return CollectionResult(
            items_read=calls,
            items_saved=bars,
            partial=bool(stopped) or counts[PARTIAL] > 0 or counts[ERROR] > 0,
            warnings=warnings,
            detail=(
                f"{len(names)} names x {len(days)} days, {len(todo)} to fetch: "
                f"{counts[COMPLETE]} complete, {counts[PARTIAL]} partial, {counts[EMPTY]} empty, "
                f"{counts[ERROR]} errors; "
                f"{calls} calls, {bars} bars new"
            ),
        )


def index_rows(code: str, day: date, page: Sequence[Any]) -> list[IndexMinuteRow]:
    key = day.strftime("%Y%m%d")
    rows: list[IndexMinuteRow] = []
    for row in page:
        if not isinstance(row, Mapping):
            raise UpstreamUnavailableError(f"KIS index row {row!r}"[:200])
        if as_text(row, "stck_bsop_date") != key:
            continue
        label = as_text(row, "stck_cntg_hour")
        # 888888 and 999999 are summary lines, not minutes.
        if len(label) != 6 or not label.isdigit() or not in_session(label):
            continue
        start = minute_start(day, label)
        rows.append(
            IndexMinuteRow(
                index_code=code,
                session_date=day,
                ts=start,
                available_at=start + timedelta(minutes=1),
                open=_number(row, "bstp_nmix_oprc"),
                high=_number(row, "bstp_nmix_hgpr"),
                low=_number(row, "bstp_nmix_lwpr"),
                close=_number(row, "bstp_nmix_prpr"),
            )
        )
    return rows


class KisIndexMinuteCollector(BaseCollector):
    """The last hundred or so minutes of KOSPI and KOSDAQ, during today's session."""

    name = "KIS_INDEX_MINUTE"

    def __init__(self, *, client: KisClient | None = None) -> None:
        self._client = client

    def is_configured(self) -> bool:
        return get_settings().kis_enabled

    def skip_reason(self) -> str:
        return "set KIS_APP_KEY and KIS_APP_SECRET (a KIS Open API app key)"

    def collect(self, session: Session) -> CollectionResult:
        with kis_run_lock():
            return self._collect(session)

    def _collect(self, session: Session) -> CollectionResult:
        calendar = MarketCalendar(Market.KR)
        today = calendar.local_today(utc_now())
        client = self._client or KisClient()
        saved = read = 0
        have: dict[str, int] = {}
        try:
            for kis_code, code in INDEXES.items():
                body, _ = client.get(
                    INDEX_PATH,
                    tr_id=INDEX_TR,
                    params={
                        "FID_COND_MRKT_DIV_CODE": "U",
                        "FID_ETC_CLS_CODE": "0",
                        "FID_INPUT_ISCD": kis_code,
                        "FID_INPUT_HOUR_1": "60",
                        "FID_PW_DATA_INCU_YN": "Y",
                    },
                )
                rows = index_rows(code, today, as_rows(body.get("output2"), source="KIS index"))
                read += len(rows)
                saved += minute_repo.save_index_bars(session, rows)
                session.flush()
                have[code] = minute_repo.index_minutes(session, code, today)
        finally:
            if self._client is None:
                client.close()
        session.commit()
        return CollectionResult(
            items_read=read,
            items_saved=saved,
            detail=f"{today}: "
            + ", ".join(f"{c} {n}/{INDEX_MINUTES} minutes" for c, n in have.items()),
        )
