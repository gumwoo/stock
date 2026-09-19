"""Time handling.

Every datetime that crosses a boundary in this system is timezone-aware UTC.
Naive datetimes are rejected at the edge rather than silently coerced, because a
naive timestamp in a point-in-time system is a correctness bug waiting to happen:
it is indistinguishable from a UTC one until it is an hour or nine hours wrong.

Use `utc_now()` rather than `datetime.now()` so that tests can freeze time with
`time-machine`. Ruff bans the stdlib calls directly (see pyproject banned-api).
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Current instant, timezone-aware, UTC."""
    return datetime.now(UTC)


def ensure_utc(value: datetime, *, field: str = "datetime") -> datetime:
    """Return `value` as UTC, rejecting naive input.

    Raises:
        ValueError: if `value` carries no timezone. This is deliberate fail-fast:
            an internal invariant violation should surface immediately, not be
            papered over by assuming UTC.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{field} must be timezone-aware; got naive {value!r}. "
            "Attach a timezone at the boundary where this value enters the system."
        )
    return value.astimezone(UTC)


def is_aware(value: datetime) -> bool:
    """True if `value` carries a usable timezone."""
    return value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None
