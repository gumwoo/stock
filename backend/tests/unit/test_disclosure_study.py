"""공시 이벤트 분석의 질문과 판정: 날짜별 짝짓기, 홀드아웃, 방향 부호."""

from __future__ import annotations

from datetime import date, timedelta

from app.scoring.disclosure_study import (
    HOLDOUT_SESSIONS,
    QUESTIONS,
    EventDay,
    by_type,
    evaluate,
    split,
    strongest,
)

START = date(2026, 6, 29)


def _event(
    n: int,
    *,
    excess: float,
    sentiment: float | None = 0.5,
    intensity: float = 0.6,
    gap: float = 0.0,
    kind: str = "SHAREHOLDER_RETURN",
    instrument: int = 1,
) -> EventDay:
    return EventDay(
        day=START + timedelta(days=n),
        instrument_id=instrument,
        event_type=kind,
        sentiment=sentiment,
        intensity=intensity,
        gap=gap,
        open_close=excess,
        index_gap=0.0,
        index_open_close=0.0,
    )


def _answers(events: list[EventDay]) -> dict[str, object]:
    return {a.key: a for a in evaluate(events)}


def test_the_questions_are_fixed() -> None:
    assert [q.key for q in QUESTIONS] == ["D1", "D2", "D3", "D4"]
    assert HOLDOUT_SESSIONS == 15


def test_the_last_sessions_are_held_out() -> None:
    days = [START + timedelta(days=n) for n in range(40)]
    study, holdout = split(days)
    assert len(holdout) == HOLDOUT_SESSIONS and len(study) == 25
    assert max(study) < min(holdout)


def test_a_steady_edge_is_established_only_if_the_holdout_agrees() -> None:
    noisy = [0.01 + (0.002 if n % 2 else -0.002) for n in range(60)]
    events = [_event(n, excess=x) for n, x in enumerate(noisy)]
    assert _answers(events)["D1"].state == "established"  # type: ignore[attr-defined]
    # 홀드아웃에서 뒤집히면 성립하지 않는다.
    flipped = [_event(n, excess=(x if n < 45 else -x)) for n, x in enumerate(noisy)]
    assert _answers(flipped)["D1"].state == "not established"  # type: ignore[attr-defined]


def test_direction_counts_only_signed_events_and_uses_their_sign() -> None:
    # 방향이 음수인 공시가 떨어지면 방향대로 움직인 것이다.
    events = [_event(n, excess=-0.01 - 0.001 * (n % 3), sentiment=-0.5) for n in range(60)] + [
        _event(n, excess=0.05, sentiment=None, instrument=2) for n in range(60)
    ]
    answers = _answers(events)
    assert answers["D2"].mean > 0  # type: ignore[attr-defined]
    # 방향 없는 공시는 D2에 들어가지 않는다: 들어가면 평균이 0.05 쪽으로 끌린다.
    assert answers["D2"].mean < 0.02  # type: ignore[attr-defined]


def test_intensity_compares_absolute_moves_on_days_with_both_groups() -> None:
    events = []
    for n in range(60):
        events.append(_event(n, excess=-0.03 - 0.001 * (n % 2), intensity=0.6, instrument=1))
        events.append(_event(n, excess=0.005, intensity=0.3, instrument=2))
    answers = _answers(events)
    assert answers["D4"].mean > 0.02  # type: ignore[attr-defined]


def test_too_few_days_decide_nothing() -> None:
    events = [_event(n, excess=0.02) for n in range(20)]
    assert all(a.state == "not enough days" for a in evaluate(events))


def test_the_strongest_filing_speaks_for_a_name_day() -> None:
    weak = _event(0, excess=0.0, intensity=0.3, kind="SHAREHOLDER_RETURN")
    strong = _event(0, excess=0.0, intensity=0.6, kind="CAPITAL_RAISE")
    assert strongest([weak, strong]) is strong


def test_the_type_table_counts_days_not_names() -> None:
    events = [_event(0, excess=0.01, instrument=i) for i in range(5)]
    events += [_event(1, excess=-0.01, instrument=9)]
    row = by_type(events)[0]
    assert (row.events, row.days) == (6, 2)
    assert row.up_share == 5 / 6
