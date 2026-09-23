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
