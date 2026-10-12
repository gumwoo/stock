"""장 초반 동반주(순수): 순위 읽기, 이름 묶음, AI 이름 확인, 관계 합치기, 알림 고르기, 시각 창."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.scoring import companions as cp

KUMHO = {1: "금호석유화학", 2: "금호에이치티", 3: "금호건설", 4: "금호전기", 5: "금호타이어"}


class TestNameGroups:
    def test_kumho_names_go_together(self) -> None:
        g = cp.name_groups({**KUMHO, 9: "형지I&C", 10: "형지글로벌", 11: "서산"})
        assert g[4] == [1, 2, 3, 5] and g[3] == [1, 2, 4, 5]
        assert g[9] == [10] and 11 not in g  # 혼자인 이름은 묶음이 없다

    def test_common_prefixes_and_big_groups_are_not_used(self) -> None:
        names = {1: "한국전력공사", 2: "한국가스공사", 3: "동양고속", 4: "동양파일"}
        names |= {10 + k: f"현대{k}호" for k in range(7)}  # 7개: 6개를 넘는 큰 묶음
        assert cp.name_groups(names) == {}

    def test_initials_and_regions_are_not_groups(self) -> None:
        names = {1: "브이씨", 2: "브이티", 3: "브이엠", 4: "경남제약", 5: "경남스틸"}
        assert cp.name_groups(names) == {}

    def test_spacs_and_reits_are_not_stocks(self) -> None:
        g = cp.name_groups({1: "한화플러스제5호스팩", 2: "금호리츠", 3: "금호건설", 4: "금호전기"})
        assert g == {3: [4], 4: [3]}

    def test_normalize_drops_marks_and_spaces(self) -> None:
        assert cp.normalize("(주)금호 전기") == "금호전기"
        assert cp.normalize("ls electric") == "LSELECTRIC"


ROWS = [
    {"stck_shrn_iscd": "002990", "hts_kor_isnm": "금호건설", "prdy_ctrt": "20.56", "stck_prpr": "13790", "oprc_vrss_prpr_rate": "14.9", "acml_vol": "11522214"},
    {"stck_shrn_iscd": "002995", "hts_kor_isnm": "금호건설우", "prdy_ctrt": "20.13", "stck_prpr": "9000", "acml_vol": "100"},
    {"stck_shrn_iscd": "001210", "hts_kor_isnm": "금호전기", "prdy_ctrt": "29.93", "stck_prpr": "11070", "acml_vol": "12562136"},
    {"stck_shrn_iscd": "0010F0", "hts_kor_isnm": "보원케미칼", "prdy_ctrt": "15.01", "stck_prpr": "3755", "acml_vol": "5893887"},
    {"stck_shrn_iscd": "123456", "hts_kor_isnm": "값없음", "prdy_ctrt": "", "acml_vol": "0"},
]  # fmt: skip


class TestRanking:
    def test_common_shares_by_change(self) -> None:
        got = cp.parse_ranking(ROWS)
        # 우선주(끝자리 5)와 등락률 없는 행은 빠지고, 새 영문 섞인 코드(끝자리 0)는 보통주로 남는다.
        assert [m.code for m in got] == ["001210", "002990", "0010F0"]
        assert got[1].open_change_pct == 14.9 and got[0].volume == 12562136

    def test_no_trades_means_not_open(self) -> None:
        quiet = [{**r, "acml_vol": "0"} for r in ROWS]
        assert not cp.traded(cp.parse_ranking(quiet)) and cp.traded(cp.parse_ranking(ROWS))

    def test_window_is_one_to_ten_minutes_after_the_open(self) -> None:
        open_at = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)  # 09:00 KST
        assert not cp.in_window(open_at + timedelta(seconds=30), open_at)
        assert cp.in_window(open_at + timedelta(minutes=1), open_at)
        assert cp.in_window(open_at + timedelta(minutes=10), open_at)
        # 09:10:00에 발화해 잠금을 잡느라 조금 늦게 시작해도 그 분은 센다.
        assert cp.in_window(open_at + timedelta(minutes=10, milliseconds=80), open_at)
        assert not cp.in_window(open_at + timedelta(minutes=11), open_at)
        late = datetime(2027, 1, 4, 1, 0, tzinfo=UTC)  # 10:00 개장일
        assert cp.in_window(late + timedelta(minutes=3), late) and not cp.in_window(
            open_at + timedelta(minutes=3), late
        )


class TestAiNames:
    def test_only_an_exact_single_match_counts(self) -> None:
        master = {"금호건설": [3], "현대자동차": [7], "대원": [8, 9]}
        got = cp.resolve_names(["금호 건설", "현대차", "대원", "없는회사"], master)
        assert got == [3, None, None, None]  # 줄임말·동명·없는 이름은 버린다


class TestMergeAndPick:
    def test_ai_from_the_same_batch_needs_the_name_rule(self) -> None:
        by_name = {4: [3, 5]}
        by_ai = {
            4: [(3, "같은 금호", True), (99, "같은 묶음 다른 회사", True), (50, "지역 연고", False)]
        }
        got = {r.instrument_id: r for r in cp.merge(4, by_name, by_ai)}
        assert got[3].sources == ("AI", "NAME") and got[3].reason == "같은 금호"
        assert 99 not in got  # 같은 질문 묶음에서 고른 AI 관계는 이름 규칙이 같이 내야 보인다
        assert got[50].sources == ("AI",) and got[5].sources == ("NAME",)

    def test_leaders_need_seven_percent_and_a_relation(self) -> None:
        m = cp.Mover
        movers = {
            4: m("001210", "금호전기", 9.0, 9280, 0.0, 10),
            20: m("000020", "관계없음", 15.0, 100, 0.0, 10),
            3: m("002990", "금호건설", 6.9, 12000, 0.0, 10),
        }
        related = {
            4: [cp.Related(3, ("NAME",)), cp.Related(5, ("NAME",))],
            3: [cp.Related(4, ("NAME",))],
        }
        picks = cp.leaders(movers, related)
        assert [p.leader_id for p in picks] == [4]  # +15%는 관계가 없고, +6.9%는 문턱 아래

    def test_companions_less_risen_first_unknown_last(self) -> None:
        rel = [
            cp.Related(1, ("NAME",)),
            cp.Related(2, ("NAME",)),
            cp.Related(3, ("AI", "NAME")),
            cp.Related(5, ("NAME",)),
        ]
        got = cp.order_companions(rel, {1: 8.0, 2: 1.0, 3: 1.0, 5: None})
        assert [r.instrument_id for r, _ in got] == [
            3,
            2,
            1,
            5,
        ]  # 같은 등락이면 두 출처 다인 것 먼저
