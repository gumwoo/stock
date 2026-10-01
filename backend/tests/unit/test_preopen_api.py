"""아침 흐름 상태 API의 순수 부분: 단계 행 만들기."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.api.preopen import ERROR_NOTE, STAGES, UNKNOWN_NOTE, korean_note, stage_rows

NOW = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)  # 09:00 KST


def test_rows_follow_the_chain_order_and_missing_stages_are_none() -> None:
    rows = stage_rows({"sweep": {"status": "SUCCESS"}}, now=NOW, opened=False)
    assert [r["name"] for r in rows] == [s[0] for s in STAGES]
    assert rows[0]["status"] == "SUCCESS" and all(r["status"] is None for r in rows[1:])
    assert rows[-1]["name"] == "snapshot" and rows[-1]["at"] == "08:38"


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
    # 모르는 형식의 긴 원문은 늘어놓지 않고 폴백 문구로, 알려진 형식은 한국어로.
    assert by["prefetch"] == UNKNOWN_NOTE
    assert by["sweep"] is None
    assert by["score"] == "08:45까지 앞 단계가 끝나지 않아 건너뜀: 기사 판정·해석(LLM)"


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

    assert time(8, 38) == preopen_service.LIST_AT
    fields = {f.name: str(f) for f in worker._KR_WATCHLIST.fields}
    assert (fields["hour"], fields["minute"]) == ("8", "38")


@pytest.mark.parametrize(
    ("name", "detail", "expected"),
    [
        # 2026-09-28 실제 기록
        ("sweep", "news PARTIAL, disclosures SUCCESS", "뉴스 일부만, 공시 완료"),
        (
            "prefetch",
            "FETCHED 19, FRESH 15, NO_DATA 1, SKIPPED_CAP 49",
            "새로 받음 19 · 이미 최신 15 · 데이터 없음 1 · 하루 상한으로 못 받음 49",
        ),
        ("prefetch", "FAILED 2, FETCHED 3", "실패 2 · 새로 받음 3"),
        ("pool", "already frozen at 22:08Z", "이미 07:08에 확정됨"),
        ("search_trends", "84 names, PARTIAL", "84종목, 일부만"),
        ("llm", "37 of 100 items", "모델에 보낸 기사 37건(상한 100)"),
        (
            "llm",
            "37 of 100 items; stopped: five-hour usage at 91%",
            "모델에 보낸 기사 37건(상한 100) · 5시간 사용량 91%에서 멈춤",
        ),
        (
            "supplement_llm",
            "0 of 30 items; stopped: seven-day usage at 88% by the last call; not starting",
            "모델에 보낸 기사 0건(상한 30) · 7일 사용량 88%라 시작하지 않음",
        ),
        (
            "llm",
            "5 of 100 items; stopped: unavailable: provider said\nsomething long",
            "모델에 보낸 기사 5건(상한 100) · LLM을 쓸 수 없어 멈춤",
        ),
        (
            "llm",
            "2 of 100 items; stopped: something new",
            "모델에 보낸 기사 2건(상한 100) · 도중에 멈춤",
        ),
        ("llm", "LLM_SCHEDULE_ENABLED is off", "예약 해석이 꺼져 있음"),
        (
            "llm",
            "0 of 100 items; stopped: another LLM run is in progress",
            "모델에 보낸 기사 0건(상한 100) · 다른 해석 작업이 돌고 있어 시작하지 않음",
        ),
        (
            "supplement_llm",
            "12 of 30 items; stopped: another LLM run is in progress",
            "모델에 보낸 기사 12건(상한 30) · 다른 해석 작업이 돌고 있어 멈춤",
        ),
        (
            "llm",
            "3 of 100 items; stopped: call quota: anthropic_daily 10/10",
            "모델에 보낸 기사 3건(상한 100) · 호출 한도로 멈춤",
        ),
        (
            "llm",
            "8 of 100 items; stopped: subscription refused: rate limited",
            "모델에 보낸 기사 8건(상한 100) · 구독에서 거절되어 멈춤",
        ),
        ("theme_news", "FAILED", "실패"),
        (
            "theme_refresh",
            "12/14 themes since 2026-09-25 06:30Z for 2026-09-28; capped at 1,000: ai; stopped: 429",
            "테마 14개 중 12개 수집 · 일부 테마는 1,000건 상한에 걸림 · 도중에 멈춤",
        ),
        ("supplement", "84 names since 22:00Z, PARTIAL", "84종목, 07:00 이후 기사, 일부만"),
        (
            "score",
            "71 scored, 13 without bars, 0 failed; 18 peers",
            "점수 낸 종목 71 · 일봉 없음 13 · 실패 0 · 비교군 18",
        ),
        ("prefetch", "pool was not frozen", "후보 풀이 확정되지 않아 건너뜀"),
        ("score", "past the open", "장이 이미 열려 건너뜀"),
        ("supplement", "the list is already frozen", "목록이 이미 확정되어 건너뜀"),
        (
            "score",
            "prerequisite not finished by 08:45: pool, prefetch",
            "08:45까지 앞 단계가 끝나지 않아 건너뜀: 후보 풀 확정, 가격·재무 사전 수집",
        ),
        ("score", "prerequisite not finished by 08:45: somewhere", UNKNOWN_NOTE),
        ("pool", "OperationalError: connection refused", ERROR_NOTE),
        ("prefetch", "FETCHED 3, WHATEVER 1", UNKNOWN_NOTE),
        ("theme_news", "a free-form collector message", UNKNOWN_NOTE),
        # 다른 단계의 형식이 엉뚱한 단계에서 풀리지 않는다(단계별로 좁힌다).
        ("sweep", "71 scored, 13 without bars, 0 failed; 18 peers", UNKNOWN_NOTE),
    ],
)
def test_stage_notes_are_korean_or_a_fallback(name: str, detail: str, expected: str) -> None:
    got = korean_note(name, detail)
    assert got == expected
    # 화면에 영어가 새지 않는다(LLM만 약어로 둔다).
    assert not [w for w in re.findall(r"[A-Za-z]+", got) if w not in ("LLM", "DB")]
