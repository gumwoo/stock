"""Health and capability endpoints.

`/health/config` is the answer to "why is there no sentiment data?". It reports
every optional capability, whether it is on, which environment variables switch
it on, and what is degraded while it is off — so a user who has filled in no
keys at all still gets a working system and a clear list of what to do next.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Diagnostics, Settings, get_settings
from app.core.clock import utc_now
from app.db import get_db
from app.models import CollectorRun

router = APIRouter(tags=["health"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_db)]


@router.get("/health")
def health() -> dict[str, Any]:
    """Liveness. Deliberately does not touch the database."""
    return {"status": "ok", "time": utc_now().isoformat()}


@router.get("/health/db")
def health_db(session: SessionDep) -> dict[str, Any]:
    """Readiness: can we actually reach Postgres?"""
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:
        return {"status": "error", "database": "unreachable", "detail": str(exc)}
    revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return {"status": "ok", "database": "reachable", "schema_revision": revision}


@router.get("/health/config")
def health_config(settings: SettingsDep) -> Diagnostics:
    """What is switched on, and what to set to switch on the rest."""
    return settings.diagnostics()


@router.get("/health/collectors")
def health_collectors(session: SessionDep) -> dict[str, Any]:
    """Most recent run per collector.

    The status here is about the *collector*, not about whether a factor is
    usable. A FAILED run does not by itself disqualify data already collected —
    that judgement belongs to freshness evaluation, which asks a different
    question per factor type.
    """
    rows = session.execute(
        select(CollectorRun).order_by(CollectorRun.source, CollectorRun.started_at.desc())
    ).scalars()

    latest: dict[str, dict[str, Any]] = {}
    for run in rows:
        if run.source in latest:
            continue
        latest[run.source] = {
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "items_read": run.items_read,
            "items_saved": run.items_saved,
            "detail": run.detail,
            "error": run.error,
        }

    return {
        "collectors": latest,
        "note": (
            "SKIPPED means no credentials were configured, which is a setup gap "
            "rather than an outage. See /health/config for what to set."
        ),
    }
