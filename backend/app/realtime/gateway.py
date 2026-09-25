"""One connection to KIS's real-time feed, relayed to every open chart.

**One connection, whoever runs.** KIS paces and counts per app key, and more
than one API worker would each open a socket. So the connection is taken only
by the process that holds a Postgres advisory lock for it; another process
serves its pages but relays nothing, and says so. The lock is held on a
connection of its own for as long as the socket is open.

**Only during the session.** The socket opens at 08:55 and closes at 15:35 on
Korean trading days; outside that the gateway waits.

**What it subscribes to** is the morning's list — up to forty names, KIS's
sample caps a connection at forty — or, on a day without one, the tracked
Korean names up to the same number, and says which.

**The earlier part of the day** is filled once per name from KIS's minute
bars, through the same reserved, paced client and the same one-KIS-run lock
as the evening's collection, so a chart opened at 11:00 starts at 09:00.

Nothing here is stored. The record is the REST minute bars fetched after the
close.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
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
from app.realtime.kis_feed import LiveBook, parse_control, parse_trades, subscribe_message
from app.repositories import instrument_repo
from app.scoring.watchlist import MAX_MEMBERS

logger = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")
OPENS = time(8, 55)
CLOSES = time(15, 35)
FEED_LOCK = "kis_ws_feed"
SEED_ATTEMPTS = 5


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


def load_members(day: date) -> tuple[str, list[LiveMember]]:
    """Today's morning list, or the tracked Korean names when there is none."""
    with session_scope() as session:
        snap = session.execute(
            select(WatchlistSnapshot).where(WatchlistSnapshot.session_date == day)
        ).scalar_one_or_none()
        found: list[LiveMember] = []
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
                        )
                    )
            return "morning list", found
        tracked = instrument_repo.list_active(session, asof=day, market=Market.KR, tracked=True)
        for n, inst in enumerate(tracked[:MAX_MEMBERS], 1):
            code = instrument_repo.current_symbol(session, inst.instrument_id)
            if code:
                found.append(
                    LiveMember(
                        inst.instrument_id, code, inst.name, n, ("TRACKED",), None, None, None
                    )
                )
        return "tracked names (no morning list today)", found


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
    ) -> None:
        self._connect = connect
        self._approval = approval
        self._clock = clock
        self._members = members
        self._seed = seed
        self._lock = lock
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
            self.book = LiveBook(day)
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
                seeding = asyncio.create_task(self._fill(day))
                try:
                    await self._relay(socket, until)
                finally:
                    seeding.cancel()
            self.status = "closed for the day"
            return True
        finally:
            await asyncio.to_thread(lock.release)

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
