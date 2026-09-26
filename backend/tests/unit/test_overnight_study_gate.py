"""밤사이 미국 업종 연구의 데이터 품질 게이트: KIS 1분봉 대조와 yfinance 내부 일치."""

from __future__ import annotations

import json
from pathlib import Path

from app.services import overnight_study_service as svc


def _write(folder: Path, name: str, data: object) -> None:
    (folder / name).write_text(json.dumps(data), encoding="utf-8")


def _world(tmp_path: Path, *, yf_close: float, daily_open: float) -> Path:
    _write(tmp_path, svc.KR_HOUR_FILE, {"000660.KS": [["2026-09-21", 100.0, yf_close]]})
    _write(
        tmp_path,
        svc.KR_DAILY_FILE,
        {"000660.KS": [["2026-09-21", daily_open, 101.0, 1000.0]]},
    )
    kis = tmp_path / "kis.json"
    kis.write_text(
        json.dumps(
            {
                "days": {
                    "7:2026-09-21": {
                        "bars": [
                            ["0900", 100.0, 101, 99, 100.5],
                            ["0959", 100.5, 102, 100, 101.0],
                            ["1000", 101.0, 103, 100, 102.0],  # 10시 봉은 비교하지 않는다
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return kis


def test_matching_sources_pass(tmp_path: Path) -> None:
    kis = _world(tmp_path, yf_close=101.0, daily_open=100.0)
    gate = svc.quality_gate(tmp_path, kis, {7: "000660.KS"})
    assert (gate.compared, gate.open_ok, gate.close_ok) == (1, 1.0, 1.0)
    assert (gate.internal_compared, gate.internal_ok) == (1, 1.0)
    assert gate.passed


def test_a_close_matching_the_ten_oclock_bar_fails(tmp_path: Path) -> None:
    # 60분봉 09:00 봉의 종가가 10:00 봉 종가와 같다면 09:00~09:59를 덮지 않는다는 뜻이다.
    kis = _world(tmp_path, yf_close=102.0, daily_open=100.0)
    gate = svc.quality_gate(tmp_path, kis, {7: "000660.KS"})
    assert gate.close_ok == 0.0 and not gate.passed


def test_an_hourly_open_that_disagrees_with_the_daily_open_fails(tmp_path: Path) -> None:
    kis = _world(tmp_path, yf_close=101.0, daily_open=105.0)
    gate = svc.quality_gate(tmp_path, kis, {7: "000660.KS"})
    assert gate.internal_ok == 0.0 and not gate.passed


def test_no_kis_file_means_no_pass(tmp_path: Path) -> None:
    _world(tmp_path, yf_close=101.0, daily_open=100.0)
    assert not svc.quality_gate(tmp_path, None, {7: "000660.KS"}).passed


def test_nxt_bars_keep_only_that_day_premarket_and_nine() -> None:
    row = {"stck_oprc": "100", "stck_hgpr": "101", "stck_lwpr": "99", "stck_prpr": "100.5"}
    body = {
        "output2": [
            {**row, "stck_bsop_date": "20251010", "stck_cntg_hour": "090000", "cntg_vol": "10"},
            {**row, "stck_bsop_date": "20251010", "stck_cntg_hour": "080000", "cntg_vol": "5"},
            {**row, "stck_bsop_date": "20251010", "stck_cntg_hour": "075900", "cntg_vol": "5"},
            {**row, "stck_bsop_date": "20251002", "stck_cntg_hour": "080100", "cntg_vol": "7"},
        ]
    }
    got = svc._nxt_bars(body, "20251010")
    assert [b[0] for b in got] == ["0800", "0900"]  # 전날 애프터마켓·08:00 전 봉은 버린다
    assert got[0] == ["0800", 100.0, 101.0, 99.0, 100.5, 5.0]
