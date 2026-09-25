"""공시 이벤트 분석 v2: 9시 시가 진입, 9시 1분부터 팔 수 있는 봉, 10시 전 종가."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.scoring.disclosure_first_hour import (
    QUESTIONS,
    STUDY_VERSION,
    FirstHour,
    MinuteBar,
    evaluate,
    measure,
)

START = date(2026, 6, 30)


def bar(label: str, o: float, h: float, lo: float, c: float) -> MinuteBar:
    return MinuteBar(label, o, h, lo, c)


def test_version_and_questions_are_fixed() -> None:
    assert STUDY_VERSION == 2
    assert [q.key for q in QUESTIONS] == ["F1", "F2", "F3", "F4"]


def test_entry_is_the_nine_oclock_open_and_exit_the_last_close_before_ten() -> None:
    bars = [
        bar("0900", 100, 130, 90, 101),  # 단일가가 섞인 봉: 고가·저가는 팔 수 없는 값
        bar("0901", 101, 104, 99, 103),
        bar("0959", 103, 106, 102, 105),
        bar("1000", 105, 120, 80, 110),  # 10시 봉은 들어가지 않는다
    ]
    ret, mfe, mae = measure(bars)  # type: ignore[misc]
    assert ret == pytest.approx(0.05)
    assert mfe == pytest.approx(0.06)
    assert mae == pytest.approx(-0.01)


def test_no_nine_oclock_bar_means_no_entry_price() -> None:
    assert measure([bar("0901", 100, 101, 99, 100)]) is None
    assert measure([bar("0900", 100, 101, 99, 100)]) is None  # 팔 수 있는 봉이 없다


def _event(
    n: int,
    *,
    ret: float,
    sentiment: float | None = 0.5,
    intensity: float = 0.6,
    mfe: float = 0.02,
    mae: float = -0.01,
    instrument: int = 1,
) -> FirstHour:
    return FirstHour(
        START + timedelta(days=n),
        instrument,
        "SHAREHOLDER_RETURN",
        sentiment,
        intensity,
        ret,
        mfe,
        mae,
    )


def test_a_steady_first_hour_gain_is_established_with_the_holdout() -> None:
    events = [_event(n, ret=0.004 + (0.001 if n % 2 else -0.001)) for n in range(60)]
    f1 = next(a for a in evaluate(events) if a.key == "F1")
    assert f1.state == "established" and f1.holdout_days == 15


def test_good_against_bad_needs_both_on_the_day() -> None:
    events = [
        _event(0, ret=0.02, sentiment=0.5),
        _event(1, ret=-0.01, sentiment=-0.5, instrument=2),
    ]
    f2 = next(a for a in evaluate(events) if a.key == "F2")
    assert f2.days == 0 and f2.holdout_days == 0


def test_room_up_against_room_down() -> None:
    events = [_event(n, ret=0.0, mfe=0.03, mae=-0.01 - 0.001 * (n % 2)) for n in range(60)]
    f4 = next(a for a in evaluate(events) if a.key == "F4")
    assert f4.mean == pytest.approx(0.0195, abs=1e-3)


def test_the_v1_sample_is_turned_into_first_hours_and_the_rest_counted() -> None:
    from app.scoring.disclosure_study import EventDay
    from app.services.disclosure_study_service import first_hour_sample

    def ev(i: int) -> EventDay:
        return EventDay(START, i, "ORDER_CONTRACT", 0.5, 0.5, 0.01, 0.0, 0.0, 0.0)

    minutes = {
        "days": {
            f"1:{START.isoformat()}": {
                "bars": [["0900", 100, 101, 99, 100], ["0901", 100, 103, 98, 102]]
            },
            f"2:{START.isoformat()}": {"error": "no symbol"},
            f"3:{START.isoformat()}": {"bars": [["0901", 100, 101, 99, 100]]},
        }
    }
    got, dropped = first_hour_sample([ev(1), ev(2), ev(3), ev(4)], minutes)
    assert [(e.instrument_id, e.ret, e.mfe, e.mae) for e in got] == [
        (1, pytest.approx(0.02), pytest.approx(0.03), pytest.approx(-0.02))
    ]
    assert dropped == {
        "fetch: no symbol": 1,
        "no 09:00 bar or nothing sellable": 1,
        "not fetched": 1,
    }
