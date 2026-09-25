"""워커 시간표: 장전 아침 흐름과 장 마감 뒤 작업이 제 시각에, 제 순서로 있는가."""

from __future__ import annotations

import pytest

from app.worker import build_scheduler


def job_ids() -> set[str]:
    return {job.id for job in build_scheduler().get_jobs()}


def test_the_jobs_are_there() -> None:
    assert {
        "preopen_morning",
        "preopen_supplement",
        "preopen_scores",
        "watchlist_before_open",
        "naver_news_after_close",
        "daily_loop_after_kr_close",
        "us_prices_after_close",
        "sec_weekly",
    } <= job_ids()


def test_the_old_morning_jobs_are_gone() -> None:
    # 08:00 스윕과 08:30 해석은 07:00 체인과 08:30 보충으로 옮겼다. 남아 있으면
    # 같은 아침에 같은 일을 두 번 한다.
    assert not {"naver_news_pre_open", "news_reading_before_open"} & job_ids()


def test_the_daily_loop_runs_after_the_korean_close_and_the_news_sweep() -> None:
    job = build_scheduler().get_job("daily_loop_after_kr_close")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"], fields["day_of_week"]) == ("16", "40", "mon-fri")


def test_the_morning_runs_in_seoul_time_in_order_before_the_open() -> None:
    scheduler = build_scheduler()
    at = {}
    for job_id in (
        "preopen_morning",
        "preopen_supplement",
        "preopen_scores",
        "watchlist_before_open",
    ):
        job = scheduler.get_job(job_id)
        assert str(job.trigger.timezone) == "Asia/Seoul"
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["day_of_week"] == "mon-fri"
        at[job_id] = (int(fields["hour"]), int(fields["minute"]))
    assert at == {
        "preopen_morning": (7, 0),
        "preopen_supplement": (8, 30),
        "preopen_scores": (8, 40),
        "watchlist_before_open": (8, 50),
    }


class _Calendar:
    def __init__(self, *, session: bool) -> None:
        self._session = session

    def __call__(self, market: object) -> _Calendar:
        return self

    def local_today(self, now: object) -> object:
        return now

    def is_session(self, day: object) -> bool:
        return self._session

    def has_closed(self, now: object) -> bool:
        return True


def _loop(monkeypatch: pytest.MonkeyPatch, *, session: bool) -> list[str]:
    from contextlib import contextmanager

    import app.worker as worker

    steps: list[str] = []

    @contextmanager
    def fake_scope():  # type: ignore[no-untyped-def]
        yield None

    def fake_run(collector: object, _session: object) -> None:
        name = type(collector).__name__
        if name == "YFinanceHistoryCollector":
            steps.append(f"prices:{collector.period}")  # type: ignore[attr-defined]
        elif name == "DartFundamentalCollector":
            steps.append(f"dart:{collector.years_back}")  # type: ignore[attr-defined]
        else:
            steps.append(name)

    monkeypatch.setattr(worker, "MarketCalendar", _Calendar(session=session))
    monkeypatch.setattr(worker, "session_scope", fake_scope)
    monkeypatch.setattr(worker, "run_collector", fake_run)
    monkeypatch.setattr(worker.scoring_service, "score_all", lambda s: steps.append("score") or [])
    monkeypatch.setattr(
        worker.regime_service, "backfill", lambda s: steps.append("regime_backfill") or 0
    )
    for fn in ("evaluate_signals", "snapshot_candidates", "evaluate_candidates"):
        monkeypatch.setattr(worker.forward_service, fn, lambda s, _fn=fn: steps.append(_fn) or 0)
    worker._daily_loop()
    return steps


def test_the_daily_loop_runs_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _loop(monkeypatch, session=True) == [
        "prices:1mo",
        "MarketIndexCollector",
        # Two business years: the collector counts back from the calendar
        # year, and this year's annual report is not filed until next March.
        "dart:2",
        "DartDisclosureCollector",
        "score",
        "regime_backfill",
        "evaluate_signals",
        "snapshot_candidates",
        "evaluate_candidates",
    ]


def test_the_daily_loop_skips_a_holiday(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _loop(monkeypatch, session=False) == []


def _sweep(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from contextlib import contextmanager

    import app.worker as worker

    steps: list[str] = []

    @contextmanager
    def fake_scope():  # type: ignore[no-untyped-def]
        yield None

    def fake_run(collector: object, _session: object) -> None:
        name = type(collector).__name__
        if name == "NaverDataLabCollector":
            name += f":{sorted(collector.instrument_ids)}"  # type: ignore[attr-defined]
        steps.append(name)

    monkeypatch.setattr(worker, "MarketCalendar", _Calendar(session=True))
    monkeypatch.setattr(worker, "session_scope", fake_scope)
    monkeypatch.setattr(worker, "run_collector", fake_run)
    monkeypatch.setattr(worker, "QuotaGuard", lambda: type("G", (), {"prune": lambda s: 0})())
    monkeypatch.setattr(worker, "NaverNewsCollector", lambda: type("NaverNewsCollector", (), {})())
    worker._collect_korean_news()
    return steps


def test_the_evening_sweep_does_not_fetch_search_trends(monkeypatch: pytest.MonkeyPatch) -> None:
    # 검색 추세는 07:00 체인이 풀 종목에 대해 받는다.
    assert _sweep(monkeypatch) == [
        "NaverNewsCollector",
        "DartDisclosureCollector",
    ]


def test_minute_bars_have_jobs_of_their_own() -> None:
    """Apart from the daily loop, so a failure in one cannot stop the other."""
    ids = job_ids()
    assert {"kis_minutes_after_close", "kis_index_minutes_0", "kis_index_minutes_1"} <= ids
    job = build_scheduler().get_job("kis_minutes_after_close")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("16", "20")
