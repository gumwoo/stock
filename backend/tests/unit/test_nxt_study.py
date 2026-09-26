"""NXT 08:00 후속 연구의 순수 규칙: 진입가, 종목일 값, 비교군, 관측, 후보 판정."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.scoring import nxt_study as n
from app.scoring import overnight_study as base


def bar(label: str, o: float, c: float, vol: float) -> n.Bar:
    return (label, o, max(o, c), min(o, c), c, vol)


class TestEntry:
    def test_volume_weighted_over_the_first_five_minutes(self) -> None:
        bars = [
            bar("0800", 100, 100, 0),  # 거래 없는 봉은 무시
            bar("0801", 100, 101, 100_000),
            bar("0803", 101, 103, 300_000),
            bar("0805", 103, 200, 1_000_000),  # 5분 밖
        ]
        assert n.entry(bars) == pytest.approx((101 * 1e5 + 103 * 3e5) / 4e5)
        assert n.first_open(bars) == 100  # 거래량 있는 첫 봉(08:01)의 시가

    def test_one_share_first_trade_does_not_set_the_price(self) -> None:
        bars = [bar("0800", 70, 70, 1), bar("0800", 100, 100, 0), bar("0801", 100, 100, 200_000)]
        assert n.entry(bars) == pytest.approx((70 + 100 * 200_000) / 200_001)
        assert n.first_open(bars) == 70  # 민감도용 값은 1주 체결에 끌린다

    def test_thin_or_late_trading_is_not_an_entry(self) -> None:
        assert n.entry([bar("0802", 100, 100, 50)]) is None  # 5,000원
        assert n.entry([bar("0805", 100, 100, 1_000_000)]) is None
        assert n.entry([bar("0802", 100, 100, 50)], min_value=0) == 100

    def test_pre_close_is_the_last_premarket_trade(self) -> None:
        bars = [bar("0801", 100, 101, 10), bar("0849", 101, 104, 10), bar("0900", 104, 110, 10)]
        assert n.pre_close(bars) == 104


def test_name_day_values_and_exclusions() -> None:
    v = n.name_day(103.0, 100.0, 104.0, 102.0)
    assert v is not None
    assert (v.g8, v.g9) == (pytest.approx(0.03), pytest.approx(0.04))
    assert v.a == pytest.approx(104 / 103 - 1) and v.b == pytest.approx(102 / 103 - 1)
    assert v.krx == pytest.approx(102 / 104 - 1)
    assert n.name_day(129.6, 100.0, 129.9, 120.0) is None  # 8시에 이미 상한가 근처
    assert n.name_day(70.4, 100.0, 90.0, 95.0) is None  # 하한가 근처 체결
    assert n.name_day(None, 100.0, 104.0, 102.0) is None


def test_control_sample_is_fixed_and_skips_listed_names() -> None:
    day = date(2025, 10, 10)
    cands = [f"{i:06d}.KS" for i in range(100)]
    a = n.control_sample(day, cands, {"000001.KS"})
    assert a == n.control_sample(day, list(reversed(cands)), {"000001.KS"})  # 입력 순서와 무관
    assert len(a) == n.CONTROL_SIZE and "000001.KS" not in a
    assert a != n.control_sample(day + timedelta(days=1), cands, {"000001.KS"})


def _nd(a: float, b: float) -> n.NameDay:
    return n.NameDay(0.01, 0.01 + a, a, b, b - a)


def test_observe_needs_two_names_and_five_controls() -> None:
    day = date(2026, 3, 3)
    lists = {"^SOX": ["A", "B"], "XLE": ["C", "D"]}
    ctrl = [f"K{i}" for i in range(5)]
    values = {"A": _nd(0.02, 0.03), "B": _nd(0.00, 0.01), "C": _nd(0.05, 0.05)}
    values |= {k: _nd(0.001, 0.002) for k in ctrl}
    obs = n.observe(day, ["^SOX", "XLE"], lists, values, ctrl)
    assert obs is not None and set(obs.by_indicator) == {"^SOX"}  # XLE는 1종목뿐
    assert obs.values["N1"] == pytest.approx(0.01 - 0.001)
    assert obs.values["N2"] == pytest.approx(0.01 - n.COST)
    assert obs.values["N4"] == pytest.approx(0.02 - 0.002)
    one = n.observe(day, ["^SOX", "XLE"], lists, values, ctrl, min_names=1)
    assert one is not None and set(one.by_indicator) == {"^SOX", "XLE"}
    few = n.observe(day, ["^SOX"], lists, values, ctrl[:4])
    assert few is not None and "N1" not in few.by_indicator["^SOX"]  # 비교군 4개: 비교값 없음


def _verdicts(ok: set[str]) -> list[base.Verdict]:
    return [
        base.Verdict(k, t, 26, 0.01, 3.0, (0.01, 0.01), 6, 0.01, "established" if k in ok else "x")
        for k, t in n.QUESTIONS
    ]


def test_candidate_needs_an_absolute_and_a_relative_pair() -> None:
    assert n.candidate(_verdicts({"N1", "N2"}))
    assert n.candidate(_verdicts({"N3", "N4"}))
    assert not n.candidate(_verdicts({"N2", "N3"}))  # 절대값만: 신호에 귀속할 수 없다
    assert not n.candidate(_verdicts({"N1", "N4"}))


def test_judge_takes_other_questions_with_the_same_rule() -> None:
    sessions = [date(2025, 9, 22) + timedelta(days=i) for i in range(245)]
    obs = []
    for i, d in enumerate(sessions[::3]):
        o = base.Observation(d, ("^SOX",))
        v = 0.006 + (0.001 if i % 2 else -0.001)
        o.by_indicator["^SOX"] = {"N1": v, "N2": -v, "N3": v, "N4": v}
        obs.append(o)
    got = {v.key: v.state for v in base.judge(obs, sessions, questions=n.QUESTIONS)}
    assert got == {
        "N1": "established",
        "N2": "not established",
        "N3": "established",
        "N4": "established",
    }
    assert [v.key for v in base.judge(obs, sessions)] == ["O1", "O2", "O3", "O4"]
