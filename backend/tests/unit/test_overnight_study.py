"""밤사이 미국 업종 연구의 순수 규칙: 정렬, 잔차, sigma, 선정, 품질 검사, 관측, 판정."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from app.scoring import overnight_study as s


def utc(y: int, m: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=UTC)


class TestAlign:
    # 한국 개장 00:00 UTC, 마감 06:30 UTC. 미국 종가 20:00 UTC(서머타임 16:00 ET).
    def kr(self, days: list[date]) -> tuple[dict, dict]:
        opens = {d: utc(d.year, d.month, d.day, 0) for d in days}
        prev = {}
        for i, d in enumerate(days):
            p = days[i - 1] if i else d - timedelta(days=1)
            prev[d] = utc(p.year, p.month, p.day, 6, 30)
        return opens, prev

    def test_one_new_us_session(self) -> None:
        # 9/7(월)의 전날 한국 장 마감은 9/4(금) 06:30 UTC. 그 뒤 끝난 미국 세션은 9/4 금요일 하나.
        days = [date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 8)]
        opens, prev = self.kr(days)
        us = [(utc(2026, 9, 3, 20), 100.0), (utc(2026, 9, 4, 20), 103.37)]
        got = s.align(days, opens, prev, us)
        assert got[date(2026, 9, 7)] == pytest.approx(0.0337)

    def test_no_new_session_is_no_signal(self) -> None:
        # 9/7(월) 미국 노동절: 9/8 한국 아침까지 새 미국 종가가 없다. 같은 +3.37%를 두 번 세지 않는다.
        days = [date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 8)]
        opens, prev = self.kr(days)
        us = [(utc(2026, 9, 3, 20), 100.0), (utc(2026, 9, 4, 20), 103.37)]
        assert s.align(days, opens, prev, us)[date(2026, 9, 8)] is None

    def test_several_sessions_after_a_korean_holiday_are_cumulated(self) -> None:
        days = [date(2026, 9, 23), date(2026, 9, 28)]
        opens, prev = self.kr(days)
        us = [
            (utc(2026, 9, 22, 20), 100.0),
            (utc(2026, 9, 23, 20), 101.0),
            (utc(2026, 9, 24, 20), 103.0),
            (utc(2026, 9, 25, 20), 104.0),
        ]
        # 9/23 마감(06:30 UTC) 전 마지막 = 9/22 종가 100, 9/28 개장 전 마지막 = 9/25 종가 104
        assert s.align(days, opens, prev, us)[date(2026, 9, 28)] == pytest.approx(0.04)


def test_residual_removes_the_market_share() -> None:
    days = [date(2024, 1, d) for d in range(1, 11)]
    mkt = {d: 0.01 * ((i % 3) - 1) for i, d in enumerate(days)}
    ind = {d: 1.5 * mkt[d] for d in days}
    beta = s.fit_beta(ind, mkt, days)
    assert beta == pytest.approx(1.5)
    assert all(abs(v) < 1e-12 for v in s.residuals(ind, mkt, beta).values())  # type: ignore[arg-type]


def test_trailing_sigma_uses_only_earlier_days_with_a_value() -> None:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(5)]
    e = {days[0]: 0.01, days[1]: None, days[2]: -0.01, days[3]: 0.03, days[4]: 0.0}
    sig = s.trailing_sigma(e, window=2)
    assert sig[days[0]] is None and sig[days[2]] is None  # 앞에 값이 2개 모이기 전
    assert sig[days[3]] == pytest.approx(math.sqrt(2) * 0.01)  # 0.01, -0.01만
    assert s.is_big(0.03, sig[days[3]]) and not s.is_calm(0.03, sig[days[3]])


def _gap_world(n_days: int = 300):  # type: ignore[no-untyped-def]
    days = [date(2023, 1, 1) + timedelta(days=i) for i in range(n_days)]
    e = {d: 0.01 * math.sin(i * 1.3) for i, d in enumerate(days)}
    linked = {
        f"L{k}": {d: 0.8 * e[d] + 0.001 * math.cos(i * (k + 2)) for i, d in enumerate(days)}
        for k in range(6)
    }
    noise = {
        f"N{k}": {d: 0.01 * math.cos(i * (k + 7) * 0.9) for i, d in enumerate(days)}
        for k in range(6)
    }
    return days, e, {**linked, **noise}


def test_selection_keeps_linked_names_and_caps_the_list() -> None:
    days, e, gaps = _gap_world()
    chosen = s.select(e, gaps, days, top=4)
    assert len(chosen) == 4 and all(n.startswith("L") for n, _ in chosen)


def test_quality_needs_five_front_names_and_a_back_half_link() -> None:
    days, e, gaps = _gap_world()
    assert s.quality("X", e, gaps, days).passed
    only_noise = {k: v for k, v in gaps.items() if k.startswith("N")}
    q = s.quality("X", e, only_noise, days)
    assert not q.passed and q.front_list < s.MIN_LIST


def test_observation_averages_indicators_equally_and_drops_limit_up() -> None:
    day = date(2026, 3, 3)
    sessions = [day - timedelta(days=i) for i in range(80, 0, -1)] + [day]
    bars = {
        "A": {day: s.DayBar(0.05, 0.02)},
        "B": {day: s.DayBar(0.01, 0.00)},
        "C": {day: s.DayBar(0.01, 0.01)},
        "U": {day: s.DayBar(0.30, 0.00)},  # 상한가 시가: 살 수 없다
    }
    lists = {"^SOX": ["A", "U"], "XLE": ["B", "C"]}
    obs = s.observe(day, ["^SOX", "XLE"], lists, bars, sessions, {})
    assert obs is not None
    # SOX 평균은 A만(0.02 - 0.003), XLE는 B·C 평균(0.005 - 0.003). 두 지표를 똑같이 섞는다.
    assert obs.values["O3"] == pytest.approx(((0.02 - 0.003) + (0.005 - 0.003)) / 2)
    assert "O4" not in obs.by_indicator["^SOX"]  # 보통 날 기록이 없다


def test_baseline_uses_only_calm_earlier_days() -> None:
    day = date(2026, 3, 3)
    sessions = [day - timedelta(days=i) for i in range(70, 0, -1)] + [day]
    bars = {"A": {d: s.DayBar(0.0, 0.01) for d in sessions}}
    bars["A"][day] = s.DayBar(0.0, 0.05)
    calm = {"A": dict.fromkeys(sessions, True)}
    assert s.baseline("A", day, sessions, bars, calm) == pytest.approx(0.01)
    calm["A"] = {d: i % 4 == 0 for i, d in enumerate(sessions)}  # 60일 중 15일만 보통 날
    assert s.baseline("A", day, sessions, bars, calm) is None


def test_judgement_holds_out_the_last_sixty_sessions() -> None:
    sessions = [date(2025, 9, 22) + timedelta(days=i) for i in range(245)]
    obs = []
    for i, d in enumerate(sessions[::3]):
        o = s.Observation(d, ("^SOX",))
        v = 0.006 + (0.001 if i % 2 else -0.001)
        o.by_indicator["^SOX"] = {"O1": v, "O2": v, "O3": v, "O4": v}
        obs.append(o)
    verdicts = {v.key: v for v in s.judge(obs, sessions)}
    assert verdicts["O3"].state == "established" and verdicts["O3"].holdout_days == 20
    assert s.build_v3(list(verdicts.values()))
    # 한 지표뿐이면 그 지표를 빼면 관측이 없다.
    assert all(v.state == "not enough days" for v in s.judge(obs, sessions, drop="^SOX"))


def test_a_calendar_session_with_no_bars_is_not_a_session() -> None:
    # XKRX는 2026-06-03(지방선거)을 세션으로 센다. 남기면 6/4의 전날이 휴장일이 된다.
    cal = [date(2026, 6, 2), date(2026, 6, 3), date(2026, 6, 4)]
    got = s.traded_sessions(cal, {date(2026, 6, 2), date(2026, 6, 4)})
    assert got == [date(2026, 6, 2), date(2026, 6, 4)]


def test_quality_rejects_a_front_list_of_three() -> None:
    days, e, gaps = _gap_world()
    three = {k: v for k, v in gaps.items() if k in {"L0", "L1", "L2"} or k.startswith("N")}
    q = s.quality("X", e, three, days)
    assert q.front_list == 3 and not q.passed


def _verdicts(held_value: float) -> list[s.Verdict]:
    sessions = [date(2025, 9, 22) + timedelta(days=i) for i in range(245)]
    held = s.holdout_days(sessions)
    obs = []
    for i, d in enumerate(sessions[::3]):
        o = s.Observation(d, ("^SOX",))
        v = held_value if d in held else 0.006 + (0.001 if i % 2 else -0.001)
        o.by_indicator["^SOX"] = {"O1": v, "O2": v, "O3": v, "O4": v}
        obs.append(o)
    return s.judge(obs, sessions)


def test_a_negative_holdout_blocks_the_verdict() -> None:
    assert all(v.state == "established" for v in _verdicts(0.001))
    assert all(v.state == "not established" for v in _verdicts(-0.001))


def test_v3_needs_both_o3_and_o4() -> None:
    ok = {v.key: v for v in _verdicts(0.001)}
    fails = next(v for v in _verdicts(-0.001) if v.key == "O4")
    assert s.build_v3(list(ok.values()))
    assert not s.build_v3([ok["O1"], ok["O2"], ok["O3"], fails])
