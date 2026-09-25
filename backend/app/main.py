"""FastAPI application — the API process.

This process serves HTTP and nothing else. It runs no scheduled jobs.

That split is deliberate rather than tidy-minded: APScheduler embedded in a web
app fires once per worker process, so the moment uvicorn runs with more than one
worker every collector runs twice. Scheduling lives in `app.worker`, which runs
as its own container, and job overlap is additionally guarded by a Postgres
advisory lock so that even two workers cannot double-fire.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import backtests, health, live, signals
from app.config import get_settings
from app.core import logging as logging_setup

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Log the capability report at startup.

    Printing this on boot means an operator sees immediately which collectors
    are inactive and why, instead of discovering it later via missing data.
    """
    settings = get_settings()
    logging_setup.configure(settings.log_level)
    diag = settings.diagnostics()
    logger.info("starting api | env=%s | %s", settings.app_env, diag["summary"])
    for item in diag["disabled"]:
        logger.info(
            "  disabled: %-18s set %s",
            item["name"],
            ", ".join(item["set_to_enable"]),
        )
    feed = None
    if settings.live_feed_enabled and settings.kis_enabled:
        # The live chart's feed. Holds KIS's socket only during the session and
        # only in the one process that takes its lock; see app/realtime/gateway.py.
        from app.realtime.gateway import Gateway

        app.state.gateway = Gateway()
        feed = asyncio.create_task(app.state.gateway.run())
        logger.info("live feed: on")
    yield
    if feed is not None:
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
    logger.info("api shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="stock",
        version="0.1.0",
        summary="Point-in-time correct stock analysis, portfolio tracking and signals",
        description=(
            "A rule-based analysis tool. It produces signals and the evidence "
            "behind them; it does not place orders and is not investment advice."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(signals.router)
    app.include_router(backtests.router)
    app.include_router(live.router)
    return app


app = create_app()


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "name": "stock",
        "docs": "/docs",
        "health": "/health",
        "config": "/health/config",
        "disclaimer": ("Rule-based analysis only. No orders are placed. Not investment advice."),
    }
