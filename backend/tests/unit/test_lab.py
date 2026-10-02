"""전략 실험실: 손절 없는 r3, 도달 판정, 구간, 날짜 기준 집계, 탐색 표시, 고정 가설 판정."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.scoring import entry_rules, lab
from app.scoring.entry_rules import Bar
from app.services.lab_service import hhmm

D0 = date(2026, 10, 5)


def bars(*rows: tuple[str, float, float, float, float]) -> tuple[Bar, ...]:
    return tuple(rows)


FLAT = bars(
    ("0900", 10000, 10000, 10000, 10000),
    ("0901", 10000, 10050, 9950, 10000),
    ("0959", 10000, 10000, 10000, 10100),
)


class TestRules:
    def test_no_stop_sells_at_the_last_close_before_ten(self) -> None:
        b = bars(
            ("0900", 10000, 10000, 10000, 10000),
            ("0901", 10000, 10000, 9000, 9100),
            ("0959", 9100, 9200, 9100, 9200),
        )
        assert entry_rules.r3(b, stop=None, take=0.05) == pytest.approx(-0.08)
        # 손절이 있으면 1호가 아래에 판다(기존 연구와 같은 체결).
        assert entry_rules.r3(b, stop=0.01, take=0.05) == pytest.approx(9890 / 10000 - 1)

    def test_target_is_rounded_up_and_must_be_passed(self) -> None:
        # +2.5% = 10250. 고가가 딱 10250이면 팔린 것으로 치지 않는다(지정가 익절은 넘어야 체결된다고 본다).
        touch = bars(("0900", 10000, 10000, 10000, 10000), ("0903", 10000, 10250, 10000, 10200))
        assert entry_rules.reached(touch, take=0.025) is None
        passed = bars(("0900", 10000, 10000, 10000, 10000), ("0903", 10000, 10260, 10000, 10200))
        assert entry_rules.reached(passed, take=0.025) == "0903"

    def test_a_stop_first_means_not_reached(self) -> None:
        b = bars(
            ("0900", 10000, 10000, 10000, 10000),
            ("0901", 10000, 10000, 9800, 9900),
            ("0902", 9900, 10300, 9900, 10300),
        )
        assert entry_rules.reached(b, take=0.025, stop=0.01) is None
        assert entry_rules.reached(b, take=0.025) == "0902"

    def test_cost_is_taken_off_in_percent(self) -> None:
        assert lab.ret(FLAT, 0.05, None) == pytest.approx((0.01 - entry_rules.COST) * 100)
        assert lab.ret(FLAT[1:], 0.05, None) is None  # 09:00 봉이 없으면 잴 수 없다

    def test_a_locked_open_could_not_be_bought(self) -> None:
        assert lab.locked(
            bars(("0900", 13000, 13000, 13000, 13000), ("0901", 13000, 13000, 13000, 13000))
        )
        assert not lab.locked(FLAT)


class TestBuckets:
    @pytest.mark.parametrize(
        ("v", "label"),
        [
            (-3.5, "-3% 미만"),
            (-3.0, "-3~-1%"),
            (-1.0, "-1~+1%"),
            (0.99, "-1~+1%"),
            (1.0, "+1~+3%"),
            (3.0, "+3% 이상"),
            (None, None),
        ],
    )
    def test_gap(self, v: float | None, label: str | None) -> None:
        assert lab.gap_bucket(v) == label

    def test_missing_scores_have_their_own_bucket(self) -> None:
        assert lab.score_bucket(None) == "점수 없음"
        assert lab.score_bucket(75) == "75 이상"


def day_sample(day: date, ret_bars: tuple[Bar, ...], **kw: object) -> lab.Sample:
    return lab.Sample(day=day, bars=ret_bars, **kw)  # type: ignore[arg-type]


def up(pct: float) -> tuple[Bar, ...]:
    end = 10000 * (1 + pct / 100)
    return bars(
        ("0900", 10000, 10000, 10000, 10000), ("0959", 10000, max(end, 10000), min(end, 10000), end)
    )


class TestDaily:
    def test_a_day_is_one_observation(self) -> None:
        # 첫날 종목 셋(+1, +1, +1), 둘째 날 하나(-3). 종목일 평균이면 0이지만 날짜 평균은 -1이다.
        s = [day_sample(D0, up(1)) for _ in range(3)] + [day_sample(D0 + timedelta(days=1), up(-3))]
        st = lab.rule_stat(s, 0.5, None)  # 목표 50%: 닿지 않고 10시에 판다
        cost = entry_rules.COST * 100
        assert st.n == 4 and st.days == 2
        assert st.mean == pytest.approx(((1 - cost) + (-3 - cost)) / 2)
        assert st.flag == "표본 부족"

    def test_flags_halves(self) -> None:
        days = [D0 + timedelta(days=i) for i in range(20)]
        same = [day_sample(d, up(2)) for d in days]
        assert lab.rule_stat(same, 0.5, None).flag == "앞뒤 같은 방향"
        split = [day_sample(d, up(2 if i < 10 else -2)) for i, d in enumerate(days)]
        assert lab.rule_stat(split, 0.5, None).flag == "앞뒤 갈림"

    def test_conditions_put_a_name_in_every_reason_it_has(self) -> None:
        s = [day_sample(D0, FLAT, reasons=("DISCLOSURE_EVENT", "SEARCH_SURGE"))]
        rows = lab.conditions(s, [("목록 이유", lambda x: x.reasons)])
        assert {r["label"] for r in rows} == {"DISCLOSURE_EVENT", "SEARCH_SURGE"}
        assert all(len(r["rules"]) == len(lab.HEADLINE) for r in rows)


class TestJudge:
    gapdown = lab.HYPOTHESES[0]

    def test_nothing_is_judged_before_twenty_days(self) -> None:
        s = [day_sample(D0 + timedelta(days=i), up(4), gap=-2.0) for i in range(5)]
        assert lab.judge(self.gapdown, s).state == "기록 중 (5/20일, 판정 아님)"

    def test_only_the_first_sixty_days_decide(self) -> None:
        # 처음 60일은 플러스(+5% 익절이거나 10시 +1%), 그 뒤 40일은 손절. 판정은 처음 60일로만.
        win = bars(("0900", 10000, 10000, 10000, 10000), ("0901", 10000, 10600, 10000, 10500))
        lose = bars(("0900", 10000, 10000, 10000, 10000), ("0901", 10000, 10000, 9800, 9800))
        s = [
            day_sample(
                D0 + timedelta(days=i), (win if i % 2 else up(1)) if i < 60 else lose, gap=-2.0
            )
            for i in range(100)
        ]
        j = lab.judge(self.gapdown, s)
        assert j.days == 60 and j.state == "성립"

    def test_names_outside_the_condition_do_not_count(self) -> None:
        s = [day_sample(D0, up(4), gap=0.5)]
        assert lab.judge(self.gapdown, s).days == 0

    def test_a_negative_prediction_is_judged_on_its_own_sign(self) -> None:
        gapup = lab.HYPOTHESES[1]
        lose = [
            day_sample(D0 + timedelta(days=i), up(-1 - (i % 3) * 0.5), gap=2.0) for i in range(60)
        ]
        assert gapup.sign == -1 and lab.judge(gapup, lose).state == "성립"


def test_the_freeze_is_before_the_first_counted_list() -> None:
    # 10/5 08:38 목록부터 센다(10/2 목록은 고정 전에 얼었다).
    assert datetime(2026, 10, 4, 23, 38, tzinfo=UTC) > lab.FROZEN_AT
    assert datetime(2026, 10, 1, 23, 38, tzinfo=UTC) < lab.FROZEN_AT


def test_minute_labels_are_seoul_time() -> None:
    assert hhmm(datetime(2026, 10, 5, 0, 3, tzinfo=UTC)) == "0903"
