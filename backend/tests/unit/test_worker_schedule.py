"""The unattended reading job exists only when the owner turned it on."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.worker import build_scheduler


def job_ids() -> set[str]:
    return {job.id for job in build_scheduler().get_jobs()}


def test_reading_news_is_not_scheduled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """It spends the owner's Claude usage with nobody at the keyboard."""
    monkeypatch.setattr(get_settings(), "llm_schedule_enabled", False)
    assert "news_reading_before_close" not in job_ids()
    assert {
        "naver_news_pre_open",
        "naver_news_after_close",
        "daily_loop_after_kr_close",
        "us_prices_after_close",
        "sec_weekly",
    } <= job_ids()


def test_the_daily_loop_runs_after_the_korean_close_and_the_news_sweep() -> None:
    job = build_scheduler().get_job("daily_loop_after_kr_close")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"], fields["day_of_week"]) == ("16", "40", "mon-fri")


def test_reading_news_runs_before_the_close_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "llm_schedule_enabled", True)
    job = build_scheduler().get_job("news_reading_before_close")
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("9", "30")
    assert str(job.trigger.timezone) == "Asia/Seoul"


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
    for fn in ("evaluate_signals", "snapshot_candidates", "evaluate_candidates"):
        monkeypatch.setattr(worker.forward_service, fn, lambda s, _fn=fn: steps.append(_fn) or 0)
    worker._daily_loop()
    return steps


def test_the_daily_loop_runs_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _loop(monkeypatch, session=True) == [
        "prices:1mo",
        # Two business years: the collector counts back from the calendar
        # year, and this year's annual report is not filed until next March.
        "dart:2",
        "score",
        "evaluate_signals",
        "snapshot_candidates",
        "evaluate_candidates",
    ]


def test_the_daily_loop_skips_a_holiday(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _loop(monkeypatch, session=False) == []
