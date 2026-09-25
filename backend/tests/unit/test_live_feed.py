"""The live feed: reading KIS's frames, folding trades into minutes, and the gateway's day.

The gateway runs against a scripted socket; no network, no database.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from app.realtime.gateway import Gateway, LiveMember
from app.realtime.kis_feed import (
    TRADE_FIELDS,
    LiveBook,
    Trade,
    minute_epoch,
    parse_control,
    parse_trades,
    subscribe_message,
)

DAY = date(2026, 9, 23)


def record(code: str, hhmmss: str, price: str, volume: str, day_volume: str = "1000") -> list[str]:
    values = ["0"] * len(TRADE_FIELDS)
    fields = dict.fromkeys(TRADE_FIELDS, "0")
    fields.update(
        MKSC_SHRN_ISCD=code,
        STCK_CNTG_HOUR=hhmmss,
        STCK_PRPR=price,
        CNTG_VOL=volume,
        ACML_VOL=day_volume,
        PRDY_CTRT="1.25",
    )
    for n, name in enumerate(TRADE_FIELDS):
        values[n] = fields[name]
    return values


def frame(*records: list[str], encrypted: str = "0", tr: str = "H0STCNT0") -> str:
    joined = "^".join(v for r in records for v in r)
    return f"{encrypted}|{tr}|{len(records):03d}|{joined}"


class TestFrames:
    def test_one_record(self) -> None:
        (t,) = parse_trades(frame(record("005930", "093512", "284500", "13")))
        assert (t.code, t.at, t.price, t.volume, t.change_pct) == (
            "005930",
            time(9, 35, 12),
            284500,
            13,
            1.25,
        )

    def test_several_records_in_one_frame(self) -> None:
        trades = parse_trades(
            frame(record("005930", "093512", "1", "1"), record("000660", "093512", "2", "2"))
        )
        assert [t.code for t in trades] == ["005930", "000660"]

    def test_encrypted_or_other_frames_are_not_trades(self) -> None:
        assert parse_trades(frame(record("005930", "093512", "1", "1"), encrypted="1")) == []
        assert parse_trades(frame(record("005930", "093512", "1", "1"), tr="H0STASP0")) == []
        assert parse_trades("not a frame") == []

    def test_a_malformed_record_is_skipped_not_fatal(self) -> None:
        bad = record("005930", "09xx12", "1", "1")
        good = record("000660", "093512", "2", "2")
        assert [t.code for t in parse_trades(frame(bad, good))] == ["000660"]

    def test_controls(self) -> None:
        ping = parse_control(
            json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20260923093000"}})
        )
        assert ping is not None and ping.tr_id == "PINGPONG"
        refused = parse_control(
            json.dumps(
                {
                    "header": {"tr_id": "H0STCNT0", "tr_key": "005930"},
                    "body": {"rt_cd": "1", "msg1": "MAX SUBSCRIBE OVER"},
                }
            )
        )
        assert refused is not None and (refused.ok, refused.code) == (False, "005930")
        assert parse_control("0|H0STCNT0|001|x") is None

    def test_the_subscription_message(self) -> None:
        sent = json.loads(subscribe_message("approval-x", "005930"))
        assert sent["header"] == {
            "approval_key": "approval-x",
            "custtype": "P",
            "tr_type": "1",
            "content-type": "utf-8",
        }
        assert sent["body"] == {"input": {"tr_id": "H0STCNT0", "tr_key": "005930"}}


def trade(hhmmss: str, price: float, volume: int) -> Trade:
    return Trade(
        "005930", time(int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])), price, volume, 0, 0.0
    )


class TestBook:
    def test_trades_in_a_minute_make_its_bar(self) -> None:
        book = LiveBook(DAY)
        book.add(trade("093501", 100, 5))
        book.add(trade("093530", 103, 2))
        bar = book.add(trade("093559", 101, 1))
        assert bar == {
            "time": minute_epoch(DAY, time(9, 35)),
            "open": 100,
            "high": 103,
            "low": 100,
            "close": 101,
            "volume": 8,
        }
        book.add(trade("093600", 99, 4))
        assert [b["time"] for b in book.series("005930")] == [
            minute_epoch(DAY, time(9, 35)),
            minute_epoch(DAY, time(9, 36)),
        ]

    def test_a_minute_is_the_start_of_that_minute_in_seoul(self) -> None:
        assert minute_epoch(DAY, time(9, 0, 59)) == int(
            datetime(2026, 9, 23, 0, 0, tzinfo=UTC).timestamp()
        )

    def test_seeded_minutes_do_not_overwrite_what_trades_built(self) -> None:
        book = LiveBook(DAY)
        book.add(trade("093501", 100, 5))
        t = minute_epoch(DAY, time(9, 35))
        book.seed("005930", [(t, 1, 1, 1, 1, 1), (t - 60, 90, 91, 89, 90, 50)])
        series = book.series("005930")
        assert series[0]["close"] == 90 and series[1]["close"] == 100


# --- the gateway's day, against a scripted socket --------------------------


class Socket:
    def __init__(self, frames: list[str | bytes], clock: list[datetime]) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.pongs: list[bytes] = []
        self.clock = clock

    async def __aenter__(self) -> Socket:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def pong(self, data: bytes) -> None:
        self.pongs.append(data)

    async def recv(self) -> str | bytes:
        if not self.frames:
            # The session ends once the script does.
            self.clock[0] = self.clock[0] + timedelta(hours=8)
            await asyncio.sleep(0)
            return "{}"
        return self.frames.pop(0)


class Lock:
    def __init__(self, free: bool = True) -> None:
        self.free = free
        self.events: list[str] = []

    def acquire(self) -> bool:
        self.events.append("acquire")
        return self.free

    def release(self) -> None:
        self.events.append("release")


MEMBERS = [LiveMember(1, "005930", "삼성전자", 1, ("TRACKED",), None, None, None)]


def gateway(socket: Socket, lock: Lock, clock: list[datetime], seeded: list[Any]) -> Gateway:
    def seed(members: Any, day: date, now: datetime) -> dict[str, Any]:
        seeded.append((len(members), day))
        return {"005930": [(minute_epoch(day, time(9, 0)), 1.0, 2.0, 0.5, 1.5, 10.0)]}

    return Gateway(
        connect=lambda url, **_: socket,
        approval=lambda: "approval-x",
        clock=lambda: clock[0],
        members=lambda day: ("morning list", MEMBERS),
        seed=seed,
        lock=lambda: lock,
    )


def test_a_session_subscribes_relays_and_answers_pings() -> None:
    clock = [datetime(2026, 9, 23, 1, 0, tzinfo=UTC)]  # 10:00 in Seoul
    socket = Socket(
        [
            frame(record("005930", "100001", "284500", "13")),
            json.dumps({"header": {"tr_id": "PINGPONG"}}),
            json.dumps(
                {
                    "header": {"tr_id": "H0STCNT0", "tr_key": "005930"},
                    "body": {"rt_cd": "1", "msg1": "no"},
                }
            ),
        ],
        clock,
    )
    lock, seeded = Lock(), []
    g = gateway(socket, lock, clock, seeded)
    heard = g.listen()

    async def run() -> bool:
        return await g.session_once()

    assert asyncio.run(run()) is True
    assert [json.loads(s)["body"]["input"]["tr_key"] for s in socket.sent] == ["005930"]
    first = heard.get_nowait()
    assert (first["type"], first["code"], first["price"]) == ("trade", "005930", 284500)
    assert socket.pongs and b"PINGPONG" in socket.pongs[0]
    assert lock.events == ["acquire", "release"]
    assert g.status == "closed for the day"
    # The refused subscription is kept, so the page can say so.
    assert g.refused == {"005930"}


def test_outside_the_session_nothing_opens() -> None:
    clock = [datetime(2026, 9, 23, 7, 0, tzinfo=UTC)]  # 16:00 in Seoul
    lock = Lock()
    g = gateway(Socket([], clock), lock, clock, [])
    assert asyncio.run(g.session_once()) is False
    assert lock.events == [] and g.status.startswith("idle")


def test_another_process_holding_the_feed_means_this_one_relays_nothing() -> None:
    clock = [datetime(2026, 9, 23, 1, 0, tzinfo=UTC)]
    socket = Socket([], clock)
    g = gateway(socket, Lock(free=False), clock, [])
    assert asyncio.run(g.session_once()) is False
    assert socket.sent == [] and g.status == "another process holds the KIS feed"


def test_a_holiday_has_no_session() -> None:
    clock = [datetime(2026, 9, 25, 1, 0, tzinfo=UTC)]  # Chuseok, 10:00 in Seoul
    g = gateway(Socket([], clock), Lock(), clock, [])
    assert g.window(clock[0]) is None
