"""신호 탭을 그날 아침 목록으로: 채점 상세의 모양, 목록 행 변환, 날짜별 API의 입력 검증."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.lists import signal_row
from app.config import get_settings
from app.main import create_app
from app.services.scoring_service import detail_of
from tests.unit.test_types import TestZeroPolicyTrap


def test_the_detail_has_the_shape_of_an_api_signal() -> None:
    signal = TestZeroPolicyTrap.build()
    d = detail_of(signal)
    json.dumps(d)  # 그대로 JSON 칸에 들어간다
    assert d["action"] == "WATCH" and d["policy"] == "ZERO" and d["strategy_version"] == "v1.7"
    assert d["decision_at"] == "2026-09-18T07:00:00+00:00"
    assert [r["text"] for r in d["reasons"]] == ["MA20 crossed upward"]
    tech = d["factors"][0]
    assert tech["engine"] == "TECHNICAL" and tech["availability"] == "AVAILABLE"
    assert tech["contribution"] == pytest.approx(72.0 * 0.40)
    assert [m["name"] for m in tech["metrics"]] == ["RSI", "MA20 distance"]
    assert (
        tech["freshness_status"] == "FRESH" and tech["source_asof"] == "2026-09-19T06:30:00+00:00"
    )
    stale = d["factors"][2]
    assert stale["availability_reason"].startswith("last post") and stale["effective_weight"] == 0.0
    # /api/signals와 같은 키
    assert set(tech) == {
        "engine",
        "score",
        "metrics",
        "requested_weight",
        "effective_weight",
        "contribution",
        "availability",
        "availability_reason",
        "source_asof",
        "source_checked_at",
        "freshness_status",
    }


def test_a_list_row_becomes_a_signal_row() -> None:
    m: Any = SimpleNamespace(
        id=5,
        instrument_id=37241,
        rank=1,
        reasons=["POSITIVE_NEWS_OVERLAY", "DISCLOSURE_EVENT"],
        total_score=40.0,
        last_action="WATCH",
        technical_score=55.0,
        fundamental_score=None,
        prefetch_status="SKIPPED_CAP",
        abstained_reason="재무: …",
        regime="RISK_ON",
        overlay_points=3.6,
        attention_surge=1.1,
        evaluated_at=datetime(2026, 9, 27, 23, 40, tzinfo=UTC),
        score_detail=None,
    )
    row = signal_row(m, "한화시스템", "272210")
    assert row["list_reasons"] == ["POSITIVE_NEWS_OVERLAY", "DISCLOSURE_EVENT"]
    assert (row["code"], row["action"], row["detail"]) == ("272210", "WATCH", None)
    assert row["evaluated_at"] == "2026-09-27T23:40:00+00:00"
    # 상세가 없으면 가중치 합도 없다. 기준값은 지금 규칙의 것.
    assert row["weight_total"] is None
    assert row["thresholds"] == {"buy_interest": 70.0, "caution": 35.0}


def test_the_weight_total_is_the_sum_of_effective_weights() -> None:
    detail = detail_of(TestZeroPolicyTrap.build())
    m: Any = SimpleNamespace(
        id=1,
        instrument_id=2,
        rank=1,
        reasons=[],
        total_score=detail["total_score"],
        last_action=detail["action"],
        technical_score=None,
        fundamental_score=None,
        prefetch_status=None,
        abstained_reason=None,
        regime=None,
        overlay_points=None,
        attention_surge=None,
        evaluated_at=None,
        score_detail=detail,
    )
    # 0.40 + 0.30 + 0 + 0.10 (빠진 심리 요인은 0)
    assert signal_row(m, "x", "1")["weight_total"] == pytest.approx(0.80)


def test_bad_interval_and_bad_day_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "live_feed_enabled", False)
    with TestClient(create_app()) as client:
        assert client.get("/api/lists/2026-09-28/bars/1?interval=5m").status_code == 400
        assert client.get("/api/lists/not-a-day/signals").status_code == 422
        # 추적 종목 채점은 멈췄다. 수동 재채점도 닫힌 기록에 신호를 섞지 않게 거절한다.
        assert client.post("/api/signals/rescore").status_code == 410
