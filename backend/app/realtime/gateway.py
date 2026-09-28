"""One connection to KIS's real-time feed, relayed to every open chart.

**One connection, whoever runs.** KIS paces and counts per app key, and more
than one API worker would each open a socket. So the connection is taken only
by the process that holds a Postgres advisory lock for it; another process
serves its pages but relays nothing, and says so. The lock is held on a
connection of its own for as long as the socket is open.

**Only during the session.** The socket opens at 08:55 and closes at 15:35 on
Korean trading days; outside that the gateway waits.

**What it subscribes to** is the morning's list — up to forty names, KIS's
sample caps a connection at forty — or, only on a day with no list at all,
the tracked Korean names up to the same number, and says which. A list with
no names is an answer, not a missing list.

**The earlier part of the day** is filled once per name from KIS's minute
bars, through the same reserved, paced client and the same one-KIS-run lock
as the evening's collection, so a chart opened at 11:00 starts at 09:00. A
socket opened before the 09:00 open fills nothing: there is nothing earlier.

**Saved:** the one-second bars, every minute and once more when the session
ends (`live_second_bar`), so a past day's chart can be looked at again. They
exist only for the time the socket was connected. The record analysed is the
REST minute bars fetched after the close.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.collectors.base import SkipCollection, UpstreamUnavailableError
from app.collectors.kis import WS_URL, KisClient
from app.collectors.kis_minute import CLOSE, STOCK_PATH, STOCK_TR, DayWalk, kis_run_lock, stock_bar
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.db import _lock_key, get_engine, session_scope
from app.models import Instrument, WatchlistMember, WatchlistSnapshot
from app.models.disclosure import Disclosure
from app.models.news import NewsItem, NewsSentiment
from app.realtime.kis_feed import LiveBook, parse_control, parse_trades, subscribe_message
from app.repositories import instrument_repo, minute_repo
from app.repositories.minute_repo import SecondBarRow
from app.scoring.watchlist import MAX_MEMBERS, STRATEGY_VERSION_V2
from app.services import overlay_service

logger = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")
OPENS = time(8, 55)
CLOSES = time(15, 35)
FEED_LOCK = "kis_ws_feed"
SEED_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class LiveEvent:
    """목록 이유 뒤의 뉴스·공시 묶음 하나(화면용). `url`은 제목을 준 기사·공시로 가는 링크, 못 찾으면 None."""

    event_type: str
    first_at: str
    title: str
    sentiment: float
    # articles는 묶음의 읽기 전체(기사 + 공시)다. 화면은 둘을 나눠 보인다.
    articles: int
    disclosures: int = 0
    url: str | None = None
    lead_source: str | None = None
    lead_id: int | None = None


@dataclass(frozen=True, slots=True)
class LiveMember:
    instrument_id: int
    code: str
    name: str
    rank: int
    reasons: tuple[str, ...]
    overlay_points: float | None
    attention_surge: float | None
    regime: str | None
    total_score: float | None = None
    prefetch_status: str | None = None
    abstained_reason: str | None = None
    events: tuple[LiveEvent, ...] = ()


# 게이트웨이가 알리는 목록 출처. 화면이 이 코드로 문구를 고른다.
MORNING_LIST = "morning list"
MORNING_LIST_EMPTY = "morning list (no names met the conditions)"
TRACKED_FALLBACK = "tracked names (no morning list today)"


def load_members(day: date) -> tuple[str, list[LiveMember]]:
    """오늘 아침 목록. 목록 자체가 없을 때만 추적 종목을 참고용으로 돌려준다.

    같은 날 V1과 V2 목록이 함께 있을 수 있어 V2를 먼저 고른다. 날짜로만 찾으면
    두 행이 나와 예외가 난다. V2 목록이 0개인 날은 0개가 답이다. "오늘 이유가
    있는 종목이 없다"를 추적 종목으로 덮으면 V1로 되돌아간다.
    """
    found: list[LiveMember] = []
    with session_scope() as session:
        snap = session.execute(
            select(WatchlistSnapshot)
            .where(WatchlistSnapshot.session_date == day)
            .order_by((WatchlistSnapshot.strategy_version == STRATEGY_VERSION_V2).desc())
            .limit(1)
        ).scalar_one_or_none()
        asof = snap.asof if snap is not None else None
        if snap is not None:
            rows = session.execute(
                select(WatchlistMember, Instrument.name)
                .join(Instrument, Instrument.instrument_id == WatchlistMember.instrument_id)
                .where(WatchlistMember.snapshot_id == snap.id)
                .order_by(WatchlistMember.rank)
            ).all()
            for m, name in rows[:MAX_MEMBERS]:
                code = instrument_repo.current_symbol(session, m.instrument_id)
                if code:
                    found.append(
                        LiveMember(
                            m.instrument_id,
                            code,
                            name,
                            m.rank,
                            tuple(m.reasons),
                            m.overlay_points,
                            m.attention_surge,
                            m.regime,
                            m.total_score,
                            m.prefetch_status,
                            m.abstained_reason,
                            to_events(m.overlay_events),
                        )
                    )
            source = MORNING_LIST if rows else MORNING_LIST_EMPTY
        else:
            tracked = instrument_repo.list_active(session, asof=day, market=Market.KR, tracked=True)
            for n, inst in enumerate(tracked[:MAX_MEMBERS], 1):
                code = instrument_repo.current_symbol(session, inst.instrument_id)
                if code:
                    found.append(
                        LiveMember(
                            inst.instrument_id, code, inst.name, n, ("TRACKED",), None, None, None
                        )
                    )
            return TRACKED_FALLBACK, found
    # 링크는 멤버를 읽은 세션을 닫은 뒤 따로 찾는다. 링크 조회의 SQL 오류가 같은 트랜잭션을 망가뜨려 목록·구독까지
    # 막지 않게 하려는 것이다(화면 보조 기능이 시세 피드를 멈추면 안 된다).
    return source, attach_links(found, asof)


def to_events(raw: object) -> tuple[LiveEvent, ...]:
    """저장된 overlay_events(JSON)를 화면용으로. 키가 빠진 옛 행이 있어도 피드를 멈추지 않게 항목마다 너그럽게 읽는다."""
    out: list[LiveEvent] = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict) or not e.get("title"):
            continue
        raw_lead = e.get("lead")
        lead: dict[str, Any] = raw_lead if isinstance(raw_lead, dict) else {}
        try:
            out.append(
                LiveEvent(
                    event_type=str(e.get("event_type") or "OTHER"),
                    first_at=str(e.get("first_at") or ""),
                    title=str(e["title"]),
                    sentiment=float(e.get("sentiment") or 0.0),
                    articles=int(e.get("articles") or 0),
                    disclosures=int(e.get("disclosures") or 0),
                    lead_source=str(lead["source"]) if lead.get("source") else None,
                    lead_id=int(lead["id"]) if lead.get("id") else None,
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(out)


DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="


def event_link(url: str | None) -> str | None:
    """화면에 걸 링크. http/https만 통과한다(저장된 문자열을 그대로 href에 넣지 않는다)."""
    if url and url.lower().startswith(("http://", "https://")):
        return url
    return None


def news_link(naver_url: str | None, url: str | None) -> str | None:
    """네이버 기사 주소를 먼저(원문이 네이버와 같으면 naver_url이 비어 있다), 없으면 원문 주소."""
    return event_link(naver_url) or event_link(url)


def attach_links(
    members: list[LiveMember],
    asof: datetime | None,
    lookup: Callable[[list[LiveMember], datetime | None], dict[tuple[int, int], str]] | None = None,
) -> list[LiveMember]:
    """이벤트마다 링크를 채운다. 조회가 어떤 이유로든 실패하면 링크 없이 그대로 돌려준다."""
    if not any(m.events for m in members):
        return members
    try:
        links = (lookup or lookup_links)(members, asof)
    except Exception:  # 링크는 보조 표시다. 목록과 구독을 막지 않는다
        logger.exception("live feed: event links failed; showing titles only")
        return members
    out = []
    for m in members:
        if not m.events:
            out.append(m)
            continue
        events = tuple(
            replace(e, url=links.get((m.instrument_id, i))) for i, e in enumerate(m.events)
        )
        out.append(replace(m, events=events))
    return out


def lookup_links(members: list[LiveMember], asof: datetime | None) -> dict[tuple[int, int], str]:
    """(종목, 이벤트 순번) → 링크. 새 세션에서 읽기만 한다."""
    window = overlay_service.PARAMS.cluster_window
    found: dict[tuple[int, int], str] = {}
    with session_scope() as session:
        news_ids = {
            e.lead_id for m in members for e in m.events if e.lead_source == "NEWS" and e.lead_id
        }
        dart_ids = {
            e.lead_id for m in members for e in m.events if e.lead_source == "DART" and e.lead_id
        }
        news = (
            {
                i: news_link(n, u)
                for i, n, u in session.execute(
                    select(NewsItem.id, NewsItem.naver_url, NewsItem.url).where(
                        NewsItem.id.in_(news_ids)
                    )
                ).all()
            }
            if news_ids
            else {}
        )
        dart = (
            {
                i: event_link(DART_VIEW + r)
                for i, r in session.execute(
                    select(Disclosure.id, Disclosure.rcept_no).where(Disclosure.id.in_(dart_ids))
                ).all()
            }
            if dart_ids
            else {}
        )
        for m in members:
            for i, e in enumerate(m.events):
                if e.lead_source == "NEWS" and e.lead_id:
                    link = news.get(e.lead_id)
                elif e.lead_source == "DART" and e.lead_id:
                    link = dart.get(e.lead_id)
                else:
                    link = _legacy_link(session, m.instrument_id, e, asof, window)
                if link:
                    found[(m.instrument_id, i)] = link
    return found


def _legacy_link(
    session: Any, instrument_id: int, event: LiveEvent, asof: datetime | None, window: timedelta
) -> str | None:
    """제목을 준 기사 id가 저장되지 않은 옛 목록(2026-09-28 목록)용. 정확하고 유일하게 맞을 때만 링크를 건다.

    다음 목록부터는 `lead`가 저장되므로 쓰이지 않는다. 지워도 된다.
    """
    if asof is None or not event.first_at:
        return None
    try:
        start = datetime.fromisoformat(event.first_at)
    except ValueError:
        return None
    if start.tzinfo is None:
        return None  # 시간대 없는 값은 비교할 수 없다. 이 이벤트만 링크 없이 둔다
    end = min(start + window, asof)
    if event.title.startswith("[공시] "):
        rcept = session.execute(
            select(Disclosure.rcept_no)
            .where(
                Disclosure.instrument_id == instrument_id,
                Disclosure.report_nm == event.title.removeprefix("[공시] "),
                Disclosure.available_at >= start,
                Disclosure.available_at <= end,
                Disclosure.ingested_at <= asof,
            )
            .order_by(Disclosure.id)
            .limit(1)
        ).scalar_one_or_none()
        if rcept:
            return event_link(DART_VIEW + rcept)
    # 저장된 제목은 앞 200자로 잘려 있다. 200자면 앞부분 일치로 찾는다.
    title_match = (
        NewsItem.title.startswith(event.title, autoescape=True)
        if len(event.title) >= 200
        else NewsItem.title == event.title
    )
    rows = session.execute(
        select(NewsItem.id, NewsItem.naver_url, NewsItem.url)
        .join(NewsSentiment, NewsSentiment.news_item_id == NewsItem.id)
        .where(
            NewsSentiment.instrument_id == instrument_id,
            NewsSentiment.created_at <= asof,
            title_match,
            NewsItem.available_at >= start,
            NewsItem.available_at <= end,
        )
        .distinct()
    ).all()
    if len(rows) != 1:
        return None
    _, naver_url, url = rows[0]
    return news_link(naver_url, url)


SAVE_EVERY = 60  # 초. 1초봉을 DB에 옮기는 주기


def seconds_to_save(
    book: LiveBook, ids: dict[str, int], saved: dict[str, int]
) -> tuple[list[SecondBarRow], dict[str, int]]:
    """아직 저장하지 않은 1초봉과, 저장에 성공하면 옮길 종목별 워터마크. 순수하다(`saved`를 바꾸지 않는다).

    이벤트 루프에서 부른다(체결이 dict를 바꾸는 곳과 같은 스레드라 도는 중에 바뀌지 않는다). 종목마다 뒤에서부터
    돌다가 워터마크보다 이른 초에서 멈춘다: dict는 넣은 순서이고 KIS 체결은 종목마다 시간순으로 온다는 가정이다.
    워터마크의 초 자체는 다시 보낸다(그 초에 늦게 온 체결을 반영하려고, 저장은 upsert).
    """
    rows: list[SecondBarRow] = []
    marks: dict[str, int] = {}
    for code, sec in book.seconds.items():
        iid = ids.get(code)
        if iid is None:
            continue
        since = saved.get(code, 0)
        mine: list[SecondBarRow] = []
        for t in reversed(sec):
            if t < since:
                break
            o, h, lo, c, v = sec[t]
            mine.append(
                SecondBarRow(
                    iid,
                    book.day,
                    datetime.fromtimestamp(t, UTC),
                    Decimal(str(o)),
                    Decimal(str(h)),
                    Decimal(str(lo)),
                    Decimal(str(c)),
                    Decimal(str(v)),
                )
            )
        if mine:
            rows.extend(mine)
            marks[code] = int(max(r.ts for r in mine).timestamp())
    return rows, marks


def write_seconds(rows: list[SecondBarRow]) -> int:
    """새 세션에서 저장하고 commit한다(게이트웨이의 다른 일과 트랜잭션을 나누지 않는다)."""
    with session_scope() as session:
        return minute_repo.save_second_bars(session, rows)


def seed_minutes(
    members: list[LiveMember], day: date, now: datetime
) -> dict[str, list[tuple[int, float, float, float, float, float]]]:
    """Today's minutes so far for each name, from KIS's minute bars.

    The one-KIS-run lock is taken per name, not for all forty at once: the
    hourly index call, which cannot be made up later, gets its turn in
    between. One name's failure is that name's gap.
    """
    label = min(now.astimezone(SEOUL).strftime("%H%M00"), CLOSE)
    out: dict[str, list[tuple[int, float, float, float, float, float]]] = {}
    with KisClient() as client:
        for m in members:
            walk = DayWalk(day)
            walk.cursor = label
            try:
                with kis_run_lock():
                    while walk.status is None:
                        body, _ = client.get(
                            STOCK_PATH,
                            tr_id=STOCK_TR,
                            params={
                                "FID_COND_MRKT_DIV_CODE": "J",
                                "FID_INPUT_ISCD": m.code,
                                "FID_INPUT_HOUR_1": walk.cursor,
                                "FID_INPUT_DATE_1": walk.key,
                                "FID_PW_DATA_INCU_YN": "Y",
                                "FID_FAKE_TICK_INCU_YN": "N",
                            },
                        )
                        walk.take(body.get("output2") or [])
                rows = [stock_bar(m.instrument_id, day, r) for r in walk.rows.values()]
            except UpstreamUnavailableError as exc:
                logger.warning("live feed: earlier minutes of %s not filled: %s", m.code, exc)
                continue
            out[m.code] = [
                (
                    int(b.ts.timestamp()),
                    float(b.open),
                    float(b.high),
                    float(b.low),
                    float(b.close),
                    float(b.volume),
                )
                for b in rows
            ]
    return out


def _approval_key() -> str:
    with KisClient() as client:
        return client.approval_key()


class FeedLock:
    """The one-connection lock, held on a database connection of its own."""

    def __init__(self) -> None:
        self._conn: Any = None

    def acquire(self) -> bool:
        conn = get_engine().connect()
        held = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _lock_key(FEED_LOCK)}
            ).scalar()
        )
        conn.commit()
        if held:
            self._conn = conn
        else:
            conn.close()
        return held

    def release(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key(FEED_LOCK)})
            self._conn.commit()
            self._conn.close()
        except Exception:  # noqa: BLE001
            # Not handed back to the pool with the lock possibly still on it.
            with contextlib.suppress(Exception):
                self._conn.invalidate()
        self._conn = None


class Gateway:
    """Holds the KIS socket through the session and fans its trades out to listeners."""

    def __init__(
        self,
        *,
        connect: Callable[..., Any] | None = None,
        approval: Callable[[], str] | None = None,
        clock: Callable[[], datetime] = utc_now,
        members: Callable[[date], tuple[str, list[LiveMember]]] = load_members,
        seed: Callable[..., dict[str, Any]] = seed_minutes,
        lock: Callable[[], FeedLock] = FeedLock,
        save_seconds: Callable[[list[SecondBarRow]], int] = write_seconds,
    ) -> None:
        self._connect = connect
        self._approval = approval
        self._clock = clock
        self._members = members
        self._seed = seed
        self._lock = lock
        self._save_seconds = save_seconds
        # 1초봉 저장 상태. book과 같은 수명이다(같은 날 재연결이면 유지, 새 날이면 비운다).
        self._ids: dict[str, int] = {}
        self._saved: dict[str, int] = {}
        self.status = "idle"
        self.source: str | None = None
        self.members: list[LiveMember] = []
        self.book: LiveBook | None = None
        self.listeners: set[asyncio.Queue[dict[str, Any]]] = set()
        self.refused: set[str] = set()
        self._connected_at: datetime | None = None

    # --- listeners ----------------------------------------------------------

    def listen(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2_000)
        self.listeners.add(queue)
        return queue

    def leave(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.listeners.discard(queue)

    def _broadcast(self, message: dict[str, Any]) -> None:
        for queue in list(self.listeners):
            with contextlib.suppress(asyncio.QueueFull):
                # A browser that stopped reading loses ticks, not the feed.
                queue.put_nowait(message)

    # --- the session ----------------------------------------------------------

    def window(self, now: datetime) -> tuple[date, datetime] | None:
        """Today and when the socket closes, if the socket should be open now."""
        calendar = MarketCalendar(Market.KR)
        day = calendar.local_today(now)
        if not calendar.is_session(day):
            return None
        local = now.astimezone(SEOUL).time()
        if not OPENS <= local < CLOSES:
            return None
        return day, datetime.combine(day, CLOSES, tzinfo=SEOUL)

    async def run(self) -> None:
        backoff = 10
        while True:
            try:
                ran = await self.session_once()
                backoff = 10
                if not ran:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a feed outage must not end the loop
                held = self._connected_at
                if held is not None and (self._clock() - held).total_seconds() > 300:
                    # A connection that held for a while and then dropped starts over.
                    backoff = 10
                self._connected_at = None
                logger.warning(
                    "live feed: %s: %s; retrying in %ss", type(exc).__name__, exc, backoff
                )
                self.status = f"reconnecting after {type(exc).__name__}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def session_once(self) -> bool:
        """Hold the feed until the session's end. False when there is nothing to do now."""
        window = self.window(self._clock())
        if window is None:
            self.status = "idle (outside the session)"
            return False
        day, until = window
        lock = self._lock()
        if not await asyncio.to_thread(lock.acquire):
            self.status = "another process holds the KIS feed"
            return False
        try:
            self.source, self.members = await asyncio.to_thread(self._members, day)
            # 같은 날 다시 연결할 때는 쌓아 둔 봉을 유지한다. 분봉은 REST로 다시 채울 수 있지만 초봉은 그럴 수 없어,
            # 새로 만들면 끊길 때마다 그날 1초봉이 사라진다. seed는 setdefault라 기존 봉을 덮지 않는다.
            if self.book is None or self.book.day != day:
                self.book = LiveBook(day)
                self._ids = {}
                self._saved = {}
            # 재연결로 멤버가 바뀌어도 앞서 받은 종목의 초봉을 저장할 수 있게 누적한다.
            self._ids.update({m.code: m.instrument_id for m in self.members})
            if not self.members:
                self.status = "no names to watch today"
                return False
            approval = await asyncio.to_thread(self._approval or _approval_key)
            connect = self._connect
            if connect is None:
                from websockets.asyncio.client import connect as ws_connect

                connect = ws_connect
            async with connect(WS_URL[get_settings().kis_env], ping_interval=None) as socket:
                for m in self.members:
                    await socket.send(subscribe_message(approval, m.code))
                    await asyncio.sleep(0.05)
                self.status = "live"
                self.refused = set()
                self._connected_at = self._clock()
                # 개장 전에 연결했으면 채울 과거 분봉이 없다. 그때 채우면 종목마다
                # 오늘 몫이 빈 REST 호출만 나간다. 장중에 늦게 연결했을 때만 채운다.
                seeding = (
                    asyncio.create_task(self._fill(day))
                    if self._clock() >= MarketCalendar(Market.KR).session_open(day)
                    else None
                )
                saver = asyncio.create_task(self._save_loop())
                try:
                    await self._relay(socket, until)
                finally:
                    if seeding is not None:
                        seeding.cancel()
                    saver.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await saver
                    # 끝나는 이유가 무엇이든(마감, 끊김, 종료) 남은 초봉을 한 번 더 저장한다.
                    await self.save_now()
            self.status = "closed for the day"
            return True
        finally:
            await asyncio.to_thread(lock.release)

    async def _save_loop(self) -> None:
        while True:
            await asyncio.sleep(SAVE_EVERY)
            await self.save_now()

    async def save_now(self) -> int:
        """아직 저장하지 않은 1초봉을 DB로 옮긴다. 실패해도 피드는 계속되고, 같은 구간을 다음에 다시 보낸다."""
        if self.book is None:
            return 0
        rows, marks = seconds_to_save(self.book, self._ids, self._saved)
        if not rows:
            return 0
        try:
            written = await asyncio.to_thread(self._save_seconds, rows)
        except Exception:  # 저장은 보조 기록이다. 시세 중계를 멈추지 않는다
            logger.exception("live feed: saving %d one-second bars failed; will retry", len(rows))
            return 0
        self._saved.update(marks)
        return written

    async def _relay(self, socket: Any, until: datetime) -> None:
        while self._clock() < until:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=30)
            except TimeoutError:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            if raw[:1] in ("0", "1"):
                assert self.book is not None
                for trade in parse_trades(raw):
                    bar = self.book.add(trade)
                    self._broadcast(
                        {
                            "type": "trade",
                            "code": trade.code,
                            "time": bar["time"] + trade.at.second,
                            "price": trade.price,
                            "volume": trade.volume,
                            "change_pct": trade.change_pct,
                            "bar": bar,
                            "sbar": dict(self.book.last_second),
                        }
                    )
                continue
            control = parse_control(raw)
            if control is None:
                continue
            if control.tr_id == "PINGPONG":
                # As KIS's sample does: answer the ping with its own text.
                await socket.pong(raw.encode())
            elif control.ok is False:
                logger.warning(
                    "live feed: %s %s refused: %s", control.tr_id, control.code, control.message
                )
                if control.code:
                    self.refused.add(control.code)
                    self.status = f"live ({len(self.refused)} subscriptions refused)"

    async def _fill(self, day: date) -> None:
        """The minutes before the chart opened, once, retrying while another KIS run holds the lock."""
        for _ in range(SEED_ATTEMPTS):
            try:
                seeded = await asyncio.to_thread(self._seed, self.members, day, self._clock())
            except SkipCollection:
                await asyncio.sleep(60)
                continue
            except Exception as exc:  # noqa: BLE001 - the chart goes on without the earlier minutes
                logger.warning(
                    "live feed: earlier minutes not filled: %s: %s", type(exc).__name__, exc
                )
                return
            assert self.book is not None
            for code, bars in seeded.items():
                self.book.seed(code, bars)
            self._broadcast({"type": "seeded", "codes": sorted(seeded)})
            return
        logger.warning("live feed: earlier minutes not filled; another KIS run held the lock")

    def state(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": self.source,
            "day": self.book.day.isoformat() if self.book else None,
            "members": [
                {
                    "instrument_id": m.instrument_id,
                    "code": m.code,
                    "name": m.name,
                    "rank": m.rank,
                    "reasons": list(m.reasons),
                    "overlay_points": m.overlay_points,
                    "attention_surge": m.attention_surge,
                    "regime": m.regime,
                    "total_score": m.total_score,
                    "prefetch_status": m.prefetch_status,
                    "abstained_reason": m.abstained_reason,
                    "events": [
                        {
                            "event_type": e.event_type,
                            "first_at": e.first_at,
                            "title": e.title,
                            "sentiment": e.sentiment,
                            "articles": e.articles,
                            "disclosures": e.disclosures,
                            "url": e.url,
                        }
                        for e in m.events
                    ],
                    "last": (
                        {
                            "price": t.price,
                            "change_pct": t.change_pct,
                            "day_volume": t.day_volume,
                        }
                        if self.book and (t := self.book.last.get(m.code))
                        else None
                    ),
                }
                for m in self.members
            ],
        }
