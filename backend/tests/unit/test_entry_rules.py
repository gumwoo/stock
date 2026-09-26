"""진입·청산 규칙의 순수 규칙: 호가표, R0~R3 체결가, 봉 처리, 하루 값, 판정."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.scoring import entry_rules as er


def bar(label: str, o: float, h: float, lo: float, c: float) -> er.Bar:
    return (label, o, h, lo, c)


def flat(label: str, p: float) -> er.Bar:
    return (label, p, p, p, p)


def test_tick_table_boundaries() -> None:
    assert er.tick(1_999) == 1 and er.tick(2_000) == 5
    assert er.tick(4_999) == 5 and er.tick(5_000) == 10
    assert er.tick(19_990) == 10 and er.tick(20_000) == 50
    assert er.tick(49_950) == 50 and er.tick(50_000) == 100
    assert er.tick(199_900) == 100 and er.tick(200_000) == 500
    assert er.tick(499_500) == 500 and er.tick(500_000) == 1_000
    assert er.round_up(10_301) == 10_310 and er.round_up(10_300) == 10_300


def test_r0_sells_at_the_last_bar_before_ten() -> None:
    bars = [flat("0900", 100), flat("0958", 103), flat("1000", 150)]
    assert er.r0(bars) == pytest.approx(0.03)
    assert er.r0([flat("0902", 100), flat("0958", 103)]) is None  # 09:00 봉 없음


def test_r1_decides_on_the_last_trade_by_0904_and_buys_the_next_bar() -> None:
    # 09:03 봉 종가 101(09:04 봉 없음 → 이월) > 시가 100 → 09:06 봉(0905 이후 첫 봉) 시가 102에 산다.
    bars = [
        flat("0900", 100),
        flat("0903", 101),
        bar("0906", 102, 103, 101, 103),
        flat("0950", 104),
    ]
    assert er.r1(bars) == pytest.approx(104 / 102 - 1)
    assert (
        er.r1([flat("0900", 100), flat("0904", 100), flat("0906", 102)]) is None
    )  # 같으면 안 산다
    assert er.r1([flat("0900", 100), flat("0904", 101)]) is None  # 0905 이후 봉 없음


def test_d1_compares_with_an_unconditional_buy_at_the_same_time() -> None:
    up = [flat("0900", 100), flat("0904", 101), flat("0905", 101), flat("0950", 103)]
    down = [flat("0900", 100), flat("0904", 99), flat("0905", 99), flat("0950", 99)]
    got = er.day_values({date(2026, 7, 1): [up, down]}, er.r1, against_all=er.buy_at_0905)
    # R1은 up만 산다: 103/101-1. 조건 없는 매수는 둘의 평균.
    expected = (103 / 101 - 1) - ((103 / 101 - 1) + 0.0) / 2
    assert got[date(2026, 7, 1)] == pytest.approx(expected)


def test_r2_buys_one_tick_above_the_range_or_at_a_gap_open() -> None:
    bars = [
        flat("0900", 10_000),
        bar("0902", 10_000, 10_100, 9_990, 10_050),
        bar("0907", 10_050, 10_150, 10_040, 10_140),  # 고가 10,150 > H 10,100 → 10,110에 산다
        flat("0955", 10_300),
    ]
    assert er.r2(bars) == pytest.approx(10_300 / 10_110 - 1)
    gap = [flat("0900", 10_000), flat("0901", 10_000), bar("0910", 10_500, 10_600, 10_400, 10_500)]
    assert er.r2(gap) == pytest.approx(0.0)  # 시가 10,500에 사서 그 봉 종가에 판다(뒤에 봉 없음)
    late = [flat("0900", 10_000), bar("0930", 10_000, 11_000, 10_000, 11_000)]
    assert er.r2(late) is None  # 09:29까지 못 넘음


def test_r3_stop_take_gap_and_same_bar_rules() -> None:
    e = 10_000.0
    take = [flat("0900", e), bar("0905", 10_100, 10_310, 10_050, 10_200), flat("0950", 9_000)]
    assert er.r3(take) == pytest.approx(0.03)  # 고가 > 10,300 → 10,300
    touch = [flat("0900", e), bar("0905", 10_100, 10_300, 10_050, 10_200), flat("0950", 10_100)]
    assert er.r3(touch) == pytest.approx(0.01)  # 고가 = 목표가는 체결로 보지 않는다
    stop = [flat("0900", e), bar("0905", 9_900, 9_950, 9_790, 9_850), flat("0950", 11_000)]
    assert er.r3(stop) == pytest.approx((9_800 - 10) / e - 1)  # 1호가 불리
    gap_down = [flat("0900", e), bar("0905", 9_500, 9_600, 9_400, 9_500)]
    assert er.r3(gap_down) == pytest.approx((9_500 - 10) / e - 1)  # 갭 손절도 1호가 불리
    gap_up = [flat("0900", e), bar("0905", 10_600, 10_700, 10_500, 10_600)]
    assert er.r3(gap_up) == pytest.approx(0.03)  # 시가가 목표 위여도 목표가
    both = [flat("0900", e), bar("0905", 10_000, 10_400, 9_700, 10_000)]
    assert er.r3(both) == pytest.approx((9_800 - 10) / e - 1)  # 같은 봉이면 손절
    ignore_auction = [bar("0900", e, 12_000, 8_000, e), flat("0950", 10_100)]
    assert er.r3(ignore_auction) == pytest.approx(0.01)  # 09:00 봉 고저로는 팔지 않는다


def _world(effect: float, n_days: int = 57) -> dict[date, list[list[er.Bar]]]:
    days = {}
    for i in range(n_days):
        d = date(2026, 6, 1) + timedelta(days=i)
        wobble = 0.001 * (1 if i % 2 else -1)
        close = 100 * (1 + effect + wobble)
        days[d] = [[flat("0900", 100), flat("0904", 99), flat("0905", 99), flat("0950", close)]]
    return days


def test_judgement_needs_t_and_the_holdout() -> None:
    v = {x.key: x for x in er.judge(_world(0.01))}
    assert v["E3"].state == "established" and v["E3"].holdout_days == 15
    assert v["E1"].state == "not enough days"  # R1은 한 번도 사지 않는다(09:04가 시가 아래)
    assert "R3" not in er.candidates(list(v.values()))  # D3(R3 - R0)는 0이라 성립하지 않는다
    assert all(x.state != "established" for x in er.judge(_world(-0.01)))


def test_stop_price_is_put_on_the_tick_grid_before_one_tick_down() -> None:
    # E 10,050 → S 9,849: 호가(10원)로 내린 9,840에서 1호가 아래 9,830.
    bars = [flat("0900", 10_050), bar("0905", 10_000, 10_010, 9_845, 9_900)]
    assert er.r3(bars) == pytest.approx(9_830 / 10_050 - 1)


def test_entry_ticks_raise_the_fill() -> None:
    bars = [flat("0900", 100), flat("0904", 101), flat("0905", 102), flat("0950", 104)]
    assert er.r1(bars, entry_ticks=1) == pytest.approx(104 / 103 - 1)


def _series(values: list[float]) -> dict[date, float]:
    return {date(2026, 6, 1) + timedelta(days=i): v for i, v in enumerate(values)}


def _entry_days(n: int) -> list[date]:
    return [date(2026, 6, 1) + timedelta(days=i) for i in range(n)]


def test_the_holdout_is_the_last_fifteen_entry_days() -> None:
    # 연구 42일은 양수, 마지막 15일은 음수: 홀드아웃이 끝이어야 성립하지 않는다.
    vals = [0.01 + (0.001 if i % 2 else -0.001) for i in range(42)] + [-0.01] * 15
    v = er.judge_one("X", "", _series(vals), _entry_days(57))
    assert v.holdout_days == 15 and v.holdout_mean is not None and v.holdout_mean < 0
    assert v.state == "not established"


def test_t_must_reach_two_and_a_half() -> None:
    # 평균 0.001, 표준편차 약 0.0028: t 약 2.3이면 성립하지 않는다.
    base = [0.001 + (0.0028 if i % 2 else -0.0028) for i in range(42)]
    v = er.judge_one("X", "", _series([*base, 0.01] * 1 + [0.01] * 14), _entry_days(57))
    assert v.t is not None and 2.0 < v.t < er.MIN_T and v.state == "not established"


def test_both_halves_must_be_positive() -> None:
    vals = [-0.001 + (0.0001 if i % 2 else -0.0001) for i in range(21)]
    vals += [0.03 + (0.0001 if i % 2 else -0.0001) for i in range(21)]
    v = er.judge_one("X", "", _series(vals + [0.01] * 15), _entry_days(57))
    assert v.halves[0] is not None and v.halves[0] < 0 and v.state == "not established"


def test_too_few_study_days() -> None:
    v = er.judge_one("X", "", _series([0.01] * 19 + [0.01] * 15), _entry_days(34))
    assert v.state == "not enough days"


def test_candidates_need_their_pairs() -> None:
    def verdicts(ok: set[str]) -> list[er.Verdict]:
        return [
            er.Verdict(
                k, x, 42, 0.01, 3.0, (0.01, 0.01), 15, 0.01, "established" if k in ok else "no"
            )
            for k, x in er.QUESTIONS
        ]

    assert er.candidates(verdicts({"E1", "D1'"})) == ["R1"]
    assert er.candidates(verdicts({"E1"})) == []
    assert er.candidates(verdicts({"D3"})) == []
    assert er.candidates(verdicts({"E3", "D3", "E2"})) == ["R2", "R3"]


def test_cost_is_taken_from_e_but_not_from_differences() -> None:
    bars = [flat("0900", 100), flat("0950", 101)]
    days = {date(2026, 7, 1): [bars]}
    assert er.day_values(days, er.r0)[date(2026, 7, 1)] == pytest.approx(0.01 - er.COST)
    assert er.day_values(days, er.r3, minus=er.r0)[date(2026, 7, 1)] == pytest.approx(0.0)


def test_range_high_ignores_the_0900_high_but_includes_the_open() -> None:
    bars = [
        bar("0900", 10_000, 10_500, 9_900, 10_000),  # 09:00 봉 고가는 범위에 넣지 않는다
        flat("0902", 10_000),
        bar("0906", 10_000, 10_050, 10_000, 10_050),  # H = 10,000을 넘음 → 10,010에 산다
        flat("0950", 10_100),
    ]
    assert er.r2(bars) == pytest.approx(10_100 / 10_010 - 1)


def test_take_profit_rounds_up_and_exit_is_the_last_bar() -> None:
    # E 10,005 → 목표 10,305.15, 10원 호가로 올림 10,310.
    bars = [flat("0900", 10_005), bar("0905", 10_100, 10_309, 10_050, 10_200), flat("0958", 10_150)]
    assert er.r3(bars) == pytest.approx(10_150 / 10_005 - 1)  # 10,309는 목표 미달, 마지막 봉에 판다
    hit = [flat("0900", 10_005), bar("0905", 10_100, 10_320, 10_050, 10_200), flat("0958", 9_000)]
    assert er.r3(hit) == pytest.approx(10_310 / 10_005 - 1)
    r1_exit = [
        flat("0900", 100),
        flat("0904", 101),
        flat("0905", 101),
        flat("0930", 90),
        flat("0958", 103),
    ]
    assert er.r1(r1_exit) == pytest.approx(103 / 101 - 1)  # 진입 다음 봉이 아니라 마지막 봉


def test_range_high_includes_the_open_so_a_bounce_below_it_is_no_breakout() -> None:
    bars = [flat("0900", 10_000), flat("0902", 9_800), bar("0906", 9_850, 9_900, 9_850, 9_900)]
    assert er.r2(bars) is None  # 09:01~09:04 고가(9,800)는 넘었지만 시가(10,000)는 못 넘었다


def test_the_pre_registered_constants() -> None:
    assert (er.COST, er.STOP, er.TAKE, er.MIN_T, er.HOLDOUT_DAYS, er.MIN_DAYS) == (
        0.003,
        0.02,
        0.03,
        2.5,
        15,
        20,
    )
