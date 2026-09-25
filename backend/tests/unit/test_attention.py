"""Search attention: the surge measure, the response parsing and the collector's calls."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.collectors.base import UpstreamUnavailableError
from app.collectors.naver_datalab import keywords_for, parse_series
from app.scoring.attention import DEFAULT, AttentionParams, Status, surge
from app.services.forward_service import attention_bucket

N = DEFAULT.recent_sessions + DEFAULT.baseline_sessions


def days(n: int, start: date = date(2026, 8, 3)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


class TestSurge:
    def test_recent_over_baseline_with_one_point_of_smoothing(self) -> None:
        window = days(N)
        series = {d.isoformat(): 10.0 for d in window[:20]} | {
            d.isoformat(): 43.0 for d in window[20:]
        }
        found = surge(series, window)
        assert found.status == Status.MEASURED
        assert (found.recent, found.baseline) == (43.0, 10.0)
        assert found.surge == pytest.approx(44 / 11)

    def test_the_scale_of_the_fetch_does_not_change_the_answer(self) -> None:
        window = days(N)
        base = {d.isoformat(): float(1 + i % 5) for i, d in enumerate(window)}
        loud = {k: v * 20 for k, v in base.items()}
        unsmoothed = AttentionParams(smoothing=0.0)
        assert surge(base, window, unsmoothed).surge == pytest.approx(
            surge(loud, window, unsmoothed).surge
        )

    def test_a_day_left_out_by_the_provider_counts_as_quiet(self) -> None:
        window = days(N)
        series = {d.isoformat(): 5.0 for d in window[:20]}  # nothing in the recent days
        found = surge(series, window)
        assert found.recent == 0.0
        assert found.surge == pytest.approx(1 / 6)

    def test_only_the_last_sessions_given_are_read(self) -> None:
        window = days(N + 10)
        # A spike before the window is outside both halves.
        series = {window[0].isoformat(): 100.0} | {d.isoformat(): 2.0 for d in window[10:]}
        assert surge(series, window).surge == pytest.approx(1.0)

    def test_no_points_at_all_is_unmeasured(self) -> None:
        assert surge({}, days(N)).status == Status.UNMEASURED

    def test_too_few_sessions_for_a_baseline_is_unmeasured(self) -> None:
        window = days(N - 1)
        series = {d.isoformat(): 1.0 for d in window}
        assert surge(series, window).status == Status.UNMEASURED


class TestBuckets:
    @pytest.mark.parametrize(
        ("status", "value", "label"),
        [
            (None, None, "no search data"),
            ("NO_FETCH", None, "no search data"),
            ("UNMEASURED", None, "too few searches"),
            ("MEASURED", 2.0, "search surge"),
            ("MEASURED", 1.99, "ordinary search"),
        ],
    )
    def test_each_record_lands_in_one_bucket(
        self, status: str | None, value: float | None, label: str
    ) -> None:
        assert attention_bucket(status, value) == label


def payload(points: list[Any]) -> dict[str, Any]:
    return {"results": [{"title": "x", "keywords": ["x 주가"], "data": points}]}


class TestParsing:
    def test_points_become_day_to_ratio(self) -> None:
        got = parse_series(
            payload(
                [{"period": "2026-09-22", "ratio": 100}, {"period": "2026-09-23", "ratio": 0.5}]
            )
        )
        assert got == {"2026-09-22": 100.0, "2026-09-23": 0.5}

    def test_no_points_is_an_empty_series_not_an_error(self) -> None:
        assert parse_series(payload([])) == {}

    @pytest.mark.parametrize(
        "point",
        [
            {"period": "2026-09-22", "ratio": "12"},
            {"period": "2026-09-22", "ratio": -1},
            {"period": "2026-09-22", "ratio": True},
            {"period": "2026-9-22", "ratio": 1},
            {"ratio": 1},
            "2026-09-22",
        ],
    )
    def test_a_malformed_point_is_an_upstream_error(self, point: Any) -> None:
        with pytest.raises(UpstreamUnavailableError):
            parse_series(payload([point]))

    def test_one_group_asked_one_group_answered(self) -> None:
        two = {"results": [payload([])["results"][0]] * 2}
        with pytest.raises(UpstreamUnavailableError):
            parse_series(two)

    def test_the_keywords_are_about_the_stock(self) -> None:
        assert keywords_for("원림") == ["원림 주가", "원림주가", "원림 주식"]


class TestTransport:
    """The real `_post`, against a scripted server rather than the network."""

    def _collector(self, events: list[str]) -> Any:
        from app.collectors.naver_datalab import NaverDataLabCollector

        class Guard:
            def reserve(self, group: str, endpoint: str, **_: Any) -> None:
                events.append(f"reserve:{group}:{endpoint}")

        c = NaverDataLabCollector(instrument_ids=[], guard=Guard())  # type: ignore[arg-type]
        c._client_id, c._client_secret = "id", "secret"
        return c

    def _client(self, events: list[str], status: int, body: str) -> Any:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            events.append("send")
            assert request.headers["X-NCP-APIGW-API-KEY-ID"] == "id"
            assert request.url.path == "/search-trend/v1/search"
            return httpx.Response(status, text=body, headers={"content-type": "text/plain"})

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_the_call_is_reserved_before_it_is_sent_and_plain_text_is_read_as_json(
        self,
    ) -> None:
        events: list[str] = []
        c = self._collector(events)
        body = '{"results": [{"title": "x", "data": [{"period": "2026-09-22", "ratio": 7}]}]}'
        with self._client(events, 200, body) as client:
            got = c._post(client, {"keywordGroups": []})
        assert events == ["reserve:naver_datalab:search_trend", "send"]
        assert parse_series(got) == {"2026-09-22": 7.0}

    def test_a_refusal_as_over_quota_is_our_ledger_being_wrong(self) -> None:
        from app.collectors.base import RateLimitedError

        events: list[str] = []
        c = self._collector(events)
        with self._client(events, 429, "{}") as client, pytest.raises(RateLimitedError):
            c._post(client, {})

    def test_any_other_failure_is_upstream(self) -> None:
        events: list[str] = []
        c = self._collector(events)
        with self._client(events, 500, "boom") as client, pytest.raises(UpstreamUnavailableError):
            c._post(client, {})

    def test_stale_data_has_its_own_bucket(self) -> None:
        assert attention_bucket("STALE", None) == "stale search data"
