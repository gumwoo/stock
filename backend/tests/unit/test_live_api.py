"""The live chart's API when the feed is off, and when a gateway holds a book."""

from __future__ import annotations

from datetime import date, time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.realtime.gateway import Gateway, LiveMember
from app.realtime.kis_feed import LiveBook, Trade


def test_off_unless_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "live_feed_enabled", False)
    with TestClient(create_app()) as client:
        body = client.get("/api/live").json()
        assert body["status"].startswith("off") and body["members"] == []
        assert client.get("/api/live/005930/bars").status_code == 404
        with client.websocket_connect("/ws/live") as ws:
            assert ws.receive_json()["status"].startswith("off")


def test_state_and_bars_from_the_gateways_book(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "live_feed_enabled", False)
    app = create_app()
    with TestClient(app) as client:
        gateway = Gateway()
        gateway.members = [
            LiveMember(1, "005930", "삼성전자", 1, ("TRACKED",), 2.5, None, "RISK_ON")
        ]
        gateway.book = LiveBook(date(2026, 9, 23))
        gateway.book.add(Trade("005930", time(9, 30, 5), 100.0, 3, 1000, 1.5))
        app.state.gateway = gateway
        state = client.get("/api/live").json()
        assert state["members"][0]["last"] == {
            "price": 100.0,
            "change_pct": 1.5,
            "day_volume": 1000,
        }
        bars = client.get("/api/live/005930/bars").json()
        assert len(bars) == 1 and bars[0]["close"] == 100.0


def test_a_browser_leaving_is_noticed_without_waiting_for_a_trade() -> None:
    """Outside the session no trade ever comes; the handler must still end and let go.

    Called directly with a socket that only reports the disconnect, as uvicorn
    does: it does not cancel the handler, it queues the disconnect for a
    receive that the handler has to be making.
    """
    import asyncio
    from types import SimpleNamespace

    from app.api.live import live_socket

    gateway = Gateway()

    class Leaving:
        app = SimpleNamespace(state=SimpleNamespace(gateway=gateway))

        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def accept(self) -> None:
            return None

        async def send_json(self, message: dict[str, object]) -> None:
            self.sent.append(message)

        async def receive(self) -> dict[str, object]:
            await asyncio.sleep(0.01)
            return {"type": "websocket.disconnect", "code": 1001}

        async def close(self) -> None:
            return None

    socket = Leaving()
    asyncio.run(asyncio.wait_for(live_socket(socket), timeout=2))  # type: ignore[arg-type]
    assert socket.sent[0]["type"] == "state"
    assert gateway.listeners == set()
