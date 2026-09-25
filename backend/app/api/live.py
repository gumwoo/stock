"""The live chart's API: today's names, their minutes so far, and a socket of trades.

Display only. Served from whatever the gateway holds in memory; if this
process does not hold the KIS feed, the state says so.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["live"])
OFF = "off (LIVE_FEED_ENABLED is not set)"


def _gateway(holder: Any) -> Any:
    return getattr(holder.app.state, "gateway", None)


@router.get("/api/live")
def live_state(request: Request) -> dict[str, Any]:
    gateway = _gateway(request)
    if gateway is None:
        return {
            "status": "off (LIVE_FEED_ENABLED is not set)",
            "source": None,
            "day": None,
            "members": [],
        }
    state: dict[str, Any] = gateway.state()
    return state


@router.get("/api/live/{code}/bars")
def live_bars(code: str, request: Request) -> list[dict[str, float]]:
    gateway = _gateway(request)
    if gateway is None or gateway.book is None:
        raise HTTPException(status_code=404, detail="the live feed is not running")
    bars: list[dict[str, float]] = gateway.book.series(code)
    return bars


@router.websocket("/ws/live")
async def live_socket(socket: WebSocket) -> None:
    await socket.accept()
    gateway = _gateway(socket)
    if gateway is None:
        await socket.send_json({"type": "state", "status": OFF})
        await socket.close()
        return
    queue = gateway.listen()

    async def relay() -> None:
        while True:
            await socket.send_json(await queue.get())

    async def until_gone() -> None:
        # The browser sends nothing; reading is how its leaving is noticed at
        # once, rather than at the next trade — which, outside the session,
        # never comes, and would hold the server's shutdown open.
        while True:
            message = await socket.receive()
            if message.get("type") == "websocket.disconnect":
                return

    try:
        await socket.send_json({"type": "state", **gateway.state()})
        tasks = [asyncio.create_task(relay()), asyncio.create_task(until_gone())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        gateway.leave(queue)
        with contextlib.suppress(Exception):
            await socket.close()
