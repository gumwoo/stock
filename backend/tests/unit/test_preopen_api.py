"""아침 흐름 상태 API의 순수 부분: 단계 행 만들기."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.api.preopen import NOTE_CHARS, STAGES, stage_rows

NOW = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)  # 09:00 KST


def test_rows_follow_the_chain_order_and_missing_stages_are_none() -> None:
    rows = stage_rows({"sweep": {"status": "SUCCESS"}}, now=NOW, opened=False)
    assert [r["name"] for r in rows] == [s[0] for s in STAGES]
    assert rows[0]["status"] == "SUCCESS" and all(r["status"] is None for r in rows[1:])
    assert rows[-1]["name"] == "snapshot" and rows[-1]["at"] == "08:50"


def test_no_pool_before_the_open_is_all_none_and_after_it_the_list_is_missing() -> None:
    assert all(r["status"] is None for r in stage_rows(None, now=NOW, opened=False))
    after = stage_rows(None, now=NOW, opened=True)
    assert after[-1]["status"] == "MISSING"
    assert all(r["status"] is None for r in after[:-1])


def test_a_long_running_stage_is_slow() -> None:
    old = (NOW - timedelta(minutes=61)).isoformat()
    fresh = (NOW - timedelta(minutes=5)).isoformat()
    rows = stage_rows(
        {
            "llm": {"status": "RUNNING", "started_at": old},
            "sweep": {"status": "RUNNING", "started_at": fresh},
        },
        now=NOW,
        opened=False,
    )
    by = {r["name"]: r["status"] for r in rows}
    assert by["llm"] == "SLOW" and by["sweep"] == "RUNNING"


def test_notes_only_for_failures_and_are_cut() -> None:
    long = "x" * 500
    rows = stage_rows(
        {
            "prefetch": {"status": "FAILED", "detail": long},
            "sweep": {"status": "SUCCESS", "detail": "news SUCCESS"},
            "score": {"status": "SKIPPED", "detail": "prerequisite not finished by 08:45: llm"},
        },
        now=NOW,
        opened=False,
    )
    by = {r["name"]: r["note"] for r in rows}
    assert by["prefetch"] is not None and len(by["prefetch"]) == NOTE_CHARS
    assert by["sweep"] is None
    assert by["score"] == "prerequisite not finished by 08:45: llm"


def test_unreadable_or_naive_start_times_do_not_break_the_rows() -> None:
    rows = stage_rows(
        {
            "llm": {"status": "RUNNING", "started_at": "not a time"},
            "sweep": {"status": "RUNNING", "started_at": "2026-09-27T20:00:00"},
        },
        now=NOW,
        opened=False,
    )
    by = {r["name"]: r["status"] for r in rows}
    assert by["llm"] == "RUNNING" and by["sweep"] == "RUNNING"


def test_the_list_time_matches_the_worker_cron() -> None:
    from datetime import time

    from app import worker
    from app.services import preopen_service

    assert time(8, 50) == preopen_service.LIST_AT
    fields = {f.name: str(f) for f in worker._KR_WATCHLIST.fields}
    assert (fields["hour"], fields["minute"]) == ("8", "50")
