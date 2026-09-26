"""트럼프 관세 글 연구의 순수 규칙: 키워드, 창, 하루 값, 군집 회귀, 판정."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from app.scoring import trump_study as t


def kst(y: int, m: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, s, tzinfo=t.SEOUL).astimezone(UTC)


def test_tariff_in_a_link_only_does_not_count() -> None:
    assert t.is_tariff("Big TARIFFS on steel!")
    assert not t.is_tariff("Read this https://amac.us/tariffs-are-working/ great")
    assert not t.is_tariff("RT: https://www.example.com/tariff")
    assert not t.is_tariff("")
    assert not t.is_tariff(None)


def test_window_bounds_include_the_close_and_exclude_nine() -> None:
    day = date(2025, 4, 8)
    start, end = t.window(kst(2025, 4, 7, 15, 30), day)
    posts = [
        kst(2025, 4, 7, 15, 30),  # 마감 정각: 포함
        kst(2025, 4, 7, 15, 29, 59),  # 마감 전: 제외
        kst(2025, 4, 8, 8, 59, 59),  # 포함
        kst(2025, 4, 8, 9, 0),  # 제외
    ]
    assert t.count_in_windows(posts, {day: (start, end)}) == {day: 2}


def test_a_holiday_window_is_long() -> None:
    start, end = t.window(kst(2026, 9, 23, 15, 30), date(2026, 9, 28))
    assert end - start > t.LONG_WINDOW
    start, end = t.window(kst(2026, 9, 21, 15, 30), date(2026, 9, 22))
    assert end - start <= t.LONG_WINDOW


def test_day_value_needs_a_thousand_names_and_drops_limits() -> None:
    names = [(100.0, 101.0, 102.0)] * 999 + [(100.0, 130.0, 131.0)]  # 마지막은 상한가
    assert t.day_value(names) is None
    got = t.day_value([(100.0, 101.0, 102.0)] * 1000)
    assert got is not None
    assert got[0] == pytest.approx(0.01) and got[1] == pytest.approx(102 / 101 - 1)
    assert got[2] == 1000


def test_cluster_se_matches_a_hand_computation() -> None:
    # y = 1 + 2x, 잔차는 군집마다 부호가 같다.
    x = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0]
    e = [0.1, 0.1, -0.2, -0.2, 0.05, 0.05]
    y = [1 + 2 * a + b for a, b in zip(x, e, strict=True)]
    cl = ["a", "a", "b", "b", "c", "c"]
    fit = t.ols_cluster(y, {"x": x}, cl, target="x")
    xm = np.column_stack([np.ones(6), x])
    inv = np.linalg.inv(xm.T @ xm)
    beta = inv @ xm.T @ np.array(y)
    r = np.array(y) - xm @ beta
    meat = sum(np.outer(xm[i].T @ r[i], xm[i].T @ r[i]) for i in ([0, 1], [2, 3], [4, 5]))
    cov = 3 / 2 * 5 / 4 * inv @ meat @ inv
    assert fit.b == pytest.approx(beta[1])
    assert fit.t == pytest.approx(beta[1] / math.sqrt(cov[1, 1]))


def test_degenerate_columns_are_dropped_not_fatal() -> None:
    y = [1.0, 2.0, 3.0, 4.0]
    cols = {"signal": [0.0, 1.0, 0.0, 1.0], "const_col": [1.0] * 4, "dup": [0.0, 1.0, 0.0, 1.0]}
    fit = t.ols_cluster(y, cols, [1, 1, 2, 2], target="signal")
    assert fit.b is not None and set(fit.dropped) == {"const_col", "dup"}
    assert t.ols_cluster(y, {"z": [1.0] * 4}, [1, 1, 2, 2], target="z").b is None


def _days(effect: float, *, n_weeks: int = 70) -> list[t.Day]:
    """주마다 월~금. 화·목은 신호일. 같은 달 안에서 신호일 f가 `effect`만큼 높다."""
    out = []
    start = date(2025, 1, 6)  # 월요일
    rng = np.random.default_rng(7)
    for w in range(n_weeks):
        for k in range(5):
            d = start + timedelta(days=7 * w + k)
            sig = k in (1, 3)
            month_level = 0.001 * (d.month % 3)
            f = month_level + (effect if sig else 0.0) + float(rng.normal(0, 0.002))
            out.append(t.Day(d, sig, int(sig), k == 0, k == 0, -f, f, 1500))
    return out


def test_split_date_is_the_middle_signal_day() -> None:
    days = [date(2025, 1, i) for i in (2, 5, 9, 12)]
    assert t.split_date(days) == date(2025, 1, 9)


def test_judge_finds_a_clear_effect_and_the_candidate_rule_needs_both() -> None:
    v = {x.key: x for x in t.judge(_days(0.006))}
    assert v["T3"].state == "established" and v["T3"].study.b == pytest.approx(0.006, abs=5e-4)
    assert v["T1"].state == "established" and v["T1"].study.b < 0  # 방향은 보고만
    assert v["T2"].state == "established"
    assert t.candidate(list(v.values()))
    none = {x.key: x for x in t.judge(_days(0.0))}
    assert none["T3"].state == "not established"
    assert not t.candidate(list(none.values()))


def test_too_few_signal_days_is_not_enough() -> None:
    # 20주 = 100일 중 60일이 홀드아웃이라 연구 구간 신호일은 16일뿐이다.
    assert all(v.state == "not enough days" for v in t.judge(_days(0.006, n_weeks=20)))
    assert t.ols_cluster([], {}, [], target="const").b is None


def test_nan_prices_are_missing_not_averaged() -> None:
    nan = float("nan")
    names = [(100.0, 101.0, 102.0)] * 1000 + [
        (nan, 101.0, 102.0),
        (100.0, nan, 1.0),
        (100.0, 101.0, nan),
    ]
    got = t.day_value(names)
    assert got is not None and got[2] == 1000 and math.isfinite(got[0]) and math.isfinite(got[1])


def _flip_holdout(days: list[t.Day]) -> list[t.Day]:
    held = t.holdout_days([d.day for d in days])
    return [
        t.Day(d.day, d.signal, d.posts, d.long, d.monday, -d.g, -d.f, d.names)
        if d.day in held
        else d
        for d in days
    ]


def test_the_split_day_goes_to_the_back_half() -> None:
    days = _days(0.006, n_weeks=90)
    held = t.holdout_days([d.day for d in days])
    study = [d for d in days if d.day not in held]
    front, back = t.split_halves(study)
    cut = t.split_date([d.day for d in study if d.signal])
    assert cut in {d.day for d in back} and cut not in {d.day for d in front}
    assert len(front) + len(back) == len(study)


def test_a_holdout_in_the_other_direction_blocks_every_question() -> None:
    v = {x.key: x for x in t.judge(_flip_holdout(_days(0.006)))}
    assert v["T3"].holdout.b is not None and v["T3"].holdout.b < 0
    assert all(x.state == "not established" for x in v.values())


def test_t3_is_one_sided_while_t1_is_two_sided() -> None:
    v = {x.key: x for x in t.judge(_days(-0.006))}
    assert v["T3"].study.b is not None and v["T3"].study.b < 0
    assert v["T3"].state == "not established"
    assert v["T1"].state == "established"  # g = -f라 양수 방향으로 성립


def test_candidate_needs_t2_and_t3() -> None:
    fit = t.Fit(0.01, 3.0, 100, 50, ())

    def verdicts(ok: set[str]) -> list[t.Verdict]:
        return [
            t.Verdict(k, x, 30, 30, fit, (fit, fit), fit, "established" if k in ok else "no")
            for k, x in t.QUESTIONS
        ]

    assert t.candidate(verdicts({"T2", "T3"}))
    assert not t.candidate(verdicts({"T3"}))
    assert not t.candidate(verdicts({"T1", "T2"}))


def test_month_effects_do_not_pass_for_a_signal_effect() -> None:
    # 홀수 달은 신호일이 많고 첫 1시간도 높다. 신호 자체의 효과는 0이다.
    out = []
    start = date(2025, 1, 6)
    rng = np.random.default_rng(3)
    for w in range(80):
        for k in range(5):
            d = start + timedelta(days=7 * w + k)
            odd = d.month % 2 == 1
            sig = k != 4 if odd else k == 4
            f = (0.01 if odd else 0.0) + float(rng.normal(0, 0.001))
            out.append(t.Day(d, sig, int(sig), k == 0, k == 0, 0.0, f, 1500))
    fit = t.fit_difference(out, "f")
    assert fit.b is not None and abs(fit.b) < 0.001


def test_t2_subtracts_the_cost() -> None:
    days = [t.Day(date(2025, 3, i), True, 1, False, False, 0.0, 0.004, 1500) for i in range(3, 8)]
    assert t.fit_mean(days, cost=0.003).b == pytest.approx(0.001)


def test_the_holdout_is_the_last_sixty_days() -> None:
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(100)]
    assert t.holdout_days(days) == set(days[-60:])


def _weekday_world(effect_on: str) -> list[t.Day]:
    """신호가 월요일(또는 연휴 다음 날)에 몰리고, 그 날은 신호와 무관하게 첫 1시간이 높다."""
    out = []
    start = date(2025, 1, 6)
    rng = np.random.default_rng(11)
    for w in range(80):
        for k in range(5):
            d = start + timedelta(days=7 * w + k)
            monday = k == 0
            long = monday or (k == 2 and w % 4 == 0)  # 월요일이 아닌 긴 창도 섞는다
            special = monday if effect_on == "monday" else (long and not monday)
            sig = special or (k == 3 and w % 3 == 0)
            f = (0.01 if special else 0.0) + float(rng.normal(0, 0.001))
            out.append(t.Day(d, sig, int(sig), long, monday, 0.0, f, 1500))
    return out


def test_monday_and_long_window_effects_are_not_signal_effects() -> None:
    for world in ("monday", "long"):
        fit = t.fit_difference(_weekday_world(world), "f")
        assert fit.b is not None and abs(fit.b) < 0.001, world


def test_errors_are_clustered_by_week() -> None:
    # 주마다 같은 충격: 주 단위로 묶으면 표준오차가 커진다(t가 작아진다).
    out = []
    start = date(2025, 1, 6)
    rng = np.random.default_rng(5)
    for w in range(60):
        shock = float(rng.normal(0, 0.01))
        for k in range(5):
            d = start + timedelta(days=7 * w + k)
            sig = k in (1, 3)
            out.append(t.Day(d, sig, 1, k == 0, k == 0, 0.0, 0.001 * sig + shock, 1500))
    week = t.fit_mean(out)
    day = t.ols_cluster(
        [d.f - t.COST for d in out if d.signal],
        {},
        [d.day for d in out if d.signal],
        target="const",
    )
    assert week.t is not None and day.t is not None and abs(week.t) < abs(day.t)


def test_the_difference_regression_also_clusters_by_week(monkeypatch: pytest.MonkeyPatch) -> None:
    out = []
    start = date(2025, 1, 6)
    rng = np.random.default_rng(9)
    for w in range(60):
        shock = float(rng.normal(0, 0.01))
        sig_week = w % 2 == 0
        for k in range(5):
            d = start + timedelta(days=7 * w + k)
            sig = sig_week and k in (1, 2, 3)
            out.append(t.Day(d, sig, 1, k == 0, k == 0, 0.0, shock, 1500))
    week = t.fit_difference(out, "f")
    monkeypatch.setattr(t, "_week", lambda d: d)
    day = t.fit_difference(out, "f")
    assert week.t is not None and day.t is not None and abs(week.t) < abs(day.t)
