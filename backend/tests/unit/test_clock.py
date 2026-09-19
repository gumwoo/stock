"""Time handling invariants.

Naive datetimes are the quiet failure mode of a point-in-time system: they look
fine, compare fine, and are wrong by an offset nobody notices until a backtest
disagrees with reality. These tests keep the boundary closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
import time_machine

from app.core.clock import ensure_utc, is_aware, utc_now


def test_utc_now_is_timezone_aware() -> None:
    now = utc_now()
    assert is_aware(now)
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


@time_machine.travel(datetime(2026, 9, 19, 12, 0, tzinfo=UTC))
def test_utc_now_is_patchable() -> None:
    """Tests must be able to freeze time; that is why utc_now exists at all."""
    assert utc_now() == datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_utc(datetime(2026, 9, 19, 15, 30))


def test_ensure_utc_converts_rather_than_relabels() -> None:
    """A KST timestamp must convert, not merely acquire a UTC label.

    15:30 KST is 06:30 UTC. Relabelling instead of converting would shift the
    instant by nine hours, which in a daily-bar system is the difference between
    before and after the close.
    """
    kst = timezone(timedelta(hours=9))
    seoul_close = datetime(2026, 9, 19, 15, 30, tzinfo=kst)

    converted = ensure_utc(seoul_close)

    assert converted == datetime(2026, 9, 19, 6, 30, tzinfo=UTC)
    assert converted.utcoffset() == timedelta(0)


def test_ensure_utc_is_idempotent() -> None:
    value = datetime(2026, 9, 19, 6, 30, tzinfo=UTC)
    assert ensure_utc(ensure_utc(value)) == value


def test_error_names_the_field() -> None:
    """The message should say which value was wrong, not just that one was."""
    with pytest.raises(ValueError, match="filed_at"):
        ensure_utc(datetime(2026, 9, 19), field="filed_at")
