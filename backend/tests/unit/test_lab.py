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
    # 10/6 08:38 목록부터 센다(10/5는 휴장, 10/2 목록은 고정 전에 얼었다).
    assert datetime(2026, 10, 4, 23, 38, tzinfo=UTC) > lab.FROZEN_AT
    assert datetime(2026, 10, 1, 23, 38, tzinfo=UTC) < lab.FROZEN_AT


def test_minute_labels_are_seoul_time() -> None:
    assert hhmm(datetime(2026, 10, 5, 0, 3, tzinfo=UTC)) == "0903"


class TestPickTrack:
    """S3: 3개 겹침 + 기술 상위 2. 고르기·경로·판정."""

    FOUR3 = ("POSITIVE_NEWS_OVERLAY", "DISCLOSURE_EVENT", "SEARCH_SURGE")

    def cand(self, i: int, tech: float | None, *, rank: int = 1, reasons=FOUR3, excluded=None):  # type: ignore[no-untyped-def]
        return lab.Candidate(i, rank, tuple(reasons), tech, excluded)

    def test_picks_the_top_two_kept_names_with_three_reasons(self) -> None:
        picks = lab.pick_top(
            [
                self.cand(1, 60, rank=1),
                self.cand(2, 90, rank=2, excluded="PREV_SURGE"),  # 목록에서 뺀 종목은 고르지 않는다
                self.cand(3, 95, rank=3, reasons=("POSITIVE_NEWS_OVERLAY", "DISCLOSURE_EVENT")),
                self.cand(4, None, rank=4),  # 기술 점수 없음
                self.cand(5, 80, rank=5),
                self.cand(6, 70, rank=6),
            ]
        )
        assert [p.instrument_id for p in picks] == [5, 6]

    def test_bad_news_does_not_count_toward_the_three(self) -> None:
        two_and_bad = ("POSITIVE_NEWS_OVERLAY", "DISCLOSURE_EVENT", "NEGATIVE_NEWS_OVERLAY")
        assert lab.overlap(two_and_bad) == 2
        assert lab.pick_top([self.cand(1, 90, reasons=two_and_bad)]) == []

    def test_a_tie_goes_to_the_better_list_rank(self) -> None:
        picks = lab.pick_top(
            [self.cand(9, 76, rank=9), self.cand(1, 76, rank=1), self.cand(7, 82, rank=7)]
        )
        assert [p.instrument_id for p in picks] == [7, 1]

    def test_only_lists_frozen_after_the_hypothesis_and_before_the_open_count(self) -> None:
        open_at = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
        assert lab.counted(datetime(2026, 10, 7, 23, 38, tzinfo=UTC), open_at)
        assert not lab.counted(
            datetime(2026, 10, 8, 1, 17, tzinfo=UTC), open_at
        )  # 장중에 만든 목록
        assert not lab.counted(
            datetime(2026, 10, 2, 23, 38, tzinfo=UTC), datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
        )  # 고정 전
        # 10/7 목록(10:17 KST)은 고정 전이기도 하다.
        assert datetime(2026, 10, 7, 1, 17, tzinfo=UTC) < lab.S3_FROZEN_AT

    def test_peak_reports_the_first_highest_bar_and_the_dip_before_it(self) -> None:
        b = bars(
            ("0900", 10000, 10100, 9900, 10000),
            ("0901", 10000, 10000, 9700, 9800),
            ("0930", 9800, 10300, 9800, 10200),
            ("0945", 10200, 10300, 10100, 10100),
            ("1300", 10100, 10500, 10000, 10400),
            ("1520", 10400, 10400, 10300, 10300),
        )
        p = lab.peak(b)
        assert p is not None
        assert p.max_ten == pytest.approx(3.0) and p.max_ten_at == "0930"
        assert p.max_day == pytest.approx(5.0) and p.max_day_at == "1300"
        assert p.dip_before_peak == pytest.approx(-3.0) and p.low_ten == pytest.approx(-3.0)
        # 마지막 봉이 15:30 전이어도 그 봉 종가가 마감이다.
        assert p.last == pytest.approx(3.0) and p.last_at == "1520" and p.after_ten
        assert lab.peak_bucket(p.max_ten_at) == "09:30~09:59"
        assert lab.peak_bucket(p.max_day_at) == "10시 이후"

    def test_peak_without_bars_after_ten_or_without_the_open_bar(self) -> None:
        b = bars(("0900", 10000, 10200, 10000, 10100), ("0905", 10100, 10100, 10000, 10000))
        p = lab.peak(b)
        assert p is not None and not p.after_ten and p.max_day == p.max_ten
        assert p.max_day_at == "0900"  # 09:00 봉 고가도 묘사에는 들어간다
        assert lab.peak(b[1:]) is None

    def test_reached_day_uses_the_same_fill_rule_over_the_whole_day(self) -> None:
        b = bars(
            ("0900", 10000, 10300, 10000, 10000),  # 09:00 봉 안의 고가는 체결 판정에 넣지 않는다
            ("0901", 10000, 10200, 10000, 10100),  # 목표 10200에 닿기만 함: 체결 아님
            ("1100", 10100, 10210, 10100, 10200),
        )
        assert entry_rules.reached(b, take=0.02) is None
        assert lab.reached_day(b, take=0.02) == "1100"

    def test_judge_share_needs_the_days_and_handles_a_constant_series(self) -> None:
        all_hit = {D0 + timedelta(days=i): 1.0 for i in range(60)}
        j = lab.judge_share(all_hit, n=120)
        assert (
            j.state == "성립" and j.t is None and j.n == 120
        )  # 표준편차 0: t는 없지만 방향은 분명하다
        assert lab.judge_share(dict(list(all_hit.items())[:10]), n=20).state.startswith("기록 중")
        half = {D0 + timedelta(days=i): 0.5 for i in range(60)}
        assert lab.judge_share(half, n=120).state == "성립 안 함"
        mixed = {D0 + timedelta(days=i): (1.0 if i % 3 else 0.0) for i in range(60)}
        assert (
            lab.judge_share(mixed, n=120).state == "성립"
        )  # 평균 0.67, t 약 2.7, 앞·뒤 절반 모두 0.5 위
        # 뒤 절반이 0.5 아래로 갈리면 전체 평균·t가 넘어도 성립하지 않는다.
        split = {
            D0 + timedelta(days=i): 1.0 if i < 30 else (0.0 if i % 2 else 0.5) for i in range(60)
        }
        j = lab.judge_share(split, n=120)
        assert j.mean is not None and j.mean > 0 and j.second is not None and j.second < 0
        assert j.state == "성립 안 함"

    def test_sign_test_is_one_sided(self) -> None:
        assert lab.sign_test(0, 0) is None
        assert lab.sign_test(3, 0) == pytest.approx(1 / 8)
        assert lab.sign_test(1, 1) == pytest.approx(3 / 4)
