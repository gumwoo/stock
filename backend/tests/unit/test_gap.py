"""9시 전 예상 갭: KIS 응답 읽기, 판정할 수 있는 값, 목록 행 제외 이유, 카톡 문장."""

from __future__ import annotations

from datetime import date

import pytest

from app.scoring import gap
from app.services import briefing_service

# 2026-10-05(휴장일) 실측 응답의 모양(output2). 장전 동시호가 시간의 값은 이 모양에 숫자만 다르다고 본다.
RAW = {
    "antc_mkop_cls_code": "311",
    "stck_prpr": "276000",
    "stck_sdpr": "276000",
    "antc_cnpr": "284500",
    "antc_cntg_vrss_sign": "2",
    "antc_cntg_vrss": "8500",
    "antc_cntg_prdy_ctrt": "3.08",
    "antc_vol": "12345",
}


class TestQuote:
    def test_change_is_computed_from_prices_and_matches_kis(self) -> None:
        q = gap.parse(RAW)
        assert q is not None
        assert q.change_pct == pytest.approx(3.0797, abs=1e-3)
        assert q.judgeable and not q.mismatched and gap.gap_up(q)

    def test_an_unsigned_or_odd_rate_is_not_judged(self) -> None:
        # 갭다운인데 KIS 대비율이 부호 없이 오면 둘이 어긋난다 → 판정하지 않는다(갭다운 종목을 빼지 않게).
        q = gap.parse({**RAW, "antc_cnpr": "267700", "antc_cntg_prdy_ctrt": "3.01"})
        assert q is not None and q.change_pct == pytest.approx(-3.007, abs=1e-3)
        assert q.mismatched and not q.judgeable and not gap.gap_up(q)

    @pytest.mark.parametrize(
        "override",
        [{"antc_cnpr": "0"}, {"stck_sdpr": "0"}, {"antc_vol": "0"}, {"antc_cntg_prdy_ctrt": ""}],
    )
    def test_no_auction_price_yet_is_not_judged(self, override: dict[str, str]) -> None:
        q = gap.parse({**RAW, **override})
        assert q is not None and not q.judgeable and not gap.gap_up(q)

    def test_just_under_three_percent_stays(self) -> None:
        q = gap.parse({**RAW, "antc_cnpr": "284200", "antc_cntg_prdy_ctrt": "2.97"})
        assert q is not None and q.judgeable and not gap.gap_up(q)

    def test_garbage_is_none(self) -> None:
        assert gap.parse(None) is None


def _row(i: int, name: str, total: float) -> dict[str, object]:
    return {
        "instrument_id": i,
        "name": name,
        "rank": i,
        "total_score": total,
        "technical_score": total,
        "fundamental_score": total,
        "weight_total": 1.0,
        "action": "WATCH",
        "detail": None,
    }


CHECK: dict[str, object] = {
    "at": "2026-10-05T23:50:00+00:00",
    "excludes": False,
    "checked": 40,
    "judgeable": 38,
    "mismatched": 1,
    "failed": 1,
}


class TestMessages:
    check = CHECK

    def test_gapped_names_are_listed_but_not_removed(self) -> None:
        # 2026-10-12부터 목록에서 빼지 않는다: 참고 문구, 순위를 다시 매기는 메시지 없음.
        out = briefing_service.gap_messages(
            date(2026, 10, 13), self.check, [("갭업종목", 4.21)], 25
        )
        text = "\n".join(out)
        assert all(len(m) <= briefing_service.MAX_CHARS for m in out)
        assert "08:50 조회" in text and "1개(목록에서 빼지 않음) · 목록 25종목" in text
        assert "조회 40·판정 38·불일치 1·실패 1" in text and "갭 큰 종목: 갭업종목 +4.2%" in text
        assert "뺌" not in text and "다시 매김" not in text

    def test_a_day_before_the_change_says_names_were_removed(self) -> None:
        old = {k: v for k, v in self.check.items() if k != "excludes"}
        out = briefing_service.gap_messages(date(2026, 10, 12), old, [("HDC", 4.6)], 22)
        assert "1개(목록에서 뺐음)" in out[0]

    def test_nothing_gapped_says_so(self) -> None:
        out = briefing_service.gap_messages(date(2026, 10, 13), self.check, [], 28)
        assert len(out) == 1 and "예상 시가 +3% 이상 종목 없음 · 목록 28종목" in out[0]

    def test_many_mismatches_warn(self) -> None:
        bad = {**self.check, "judgeable": 5, "mismatched": 30}
        out = briefing_service.gap_messages(date(2026, 10, 6), bad, [], 28)
        assert "절반을 넘습니다" in out[0] and "남아 있을 수" not in out[0]
        # 실패가 많아도 같은 경고(판정하지 못한 수 = 조회+실패-판정).
        failing = {**self.check, "checked": 10, "judgeable": 8, "mismatched": 0, "failed": 30}
        assert (
            "절반을 넘습니다"
            in briefing_service.gap_messages(date(2026, 10, 6), failing, [], 28)[0]
        )

    def test_the_warning_no_longer_talks_about_names_left_on_the_list(self) -> None:
        out = briefing_service.gap_warning(date(2026, 10, 13), "08:50 판정 기록 없음")
        assert len(out) == 1 and "목록에 남아" not in out[0] and "확인하지 못했습니다" in out[0]

    def test_a_long_list_of_names_is_split_under_the_limit(self) -> None:
        many = [(f"종목이름{i:02d}가나다", 3.0 + i / 10) for i in range(30)]
        out = briefing_service.gap_messages(date(2026, 10, 6), self.check, many, 10)
        assert all(len(m) <= briefing_service.MAX_CHARS for m in out)
        assert sum(m.count("종목이름") for m in out) == 30

    def test_the_morning_header_notes_a_late_gap_exclusion(self) -> None:
        out = briefing_service.build(
            date(2026, 10, 6), [_row(1, "가", 70)], {}, excluded=3, gap_up=2
        )
        assert "(점수 40 미만·전일 급등 3개 뺌)(갭 +3% 2개 뺌)" in out[0]


class TestMorningExtras:
    """08:44 브리핑에 더한 것: 내 매매 원칙(소유자가 정한 것), 좋은 뉴스 종목 기술 점수 순(표시만)."""

    def test_the_principle_fits_and_says_it_is_not_advice(self) -> None:
        assert len(briefing_service.PRINCIPLE) <= briefing_service.MAX_CHARS
        assert (
            "직접 정한 것" in briefing_service.PRINCIPLE
            and "매매 권유 아님" in briefing_service.PRINCIPLE
        )

    def test_good_news_names_go_by_technical_score_and_missing_last(self) -> None:
        rows = [
            {
                **_row(1, "가", 50),
                "technical_score": 61.0,
                "list_reasons": ["POSITIVE_NEWS_OVERLAY"],
            },
            {
                **_row(2, "나", 50),
                "technical_score": None,
                "list_reasons": ["POSITIVE_NEWS_OVERLAY"],
            },
            {
                **_row(3, "다", 50),
                "technical_score": 88.0,
                "list_reasons": ["POSITIVE_NEWS_OVERLAY", "DISCLOSURE_EVENT"],
            },
            {**_row(4, "라", 50), "technical_score": 99.0, "list_reasons": ["DISCLOSURE_EVENT"]},
        ]
        out = briefing_service.good_news_messages(rows)
        assert out == [
            "좋은 뉴스 종목 3개(기술 점수 순): 다 88.0, 가 61.0, 나 점수 없음\n\n"
            + briefing_service.GOOD_NEWS_NOTE
        ]

    def test_many_good_news_names_split_under_the_limit(self) -> None:
        rows = [
            {
                **_row(i, f"아주긴종목이름{i:02d}", 50),
                "technical_score": 50.0 + i,
                "list_reasons": ["POSITIVE_NEWS_OVERLAY"],
            }
            for i in range(26)
        ]
        out = briefing_service.good_news_messages(rows)
        assert all(len(m) <= briefing_service.MAX_CHARS for m in out) and len(out) >= 3
        assert sum(m.count("아주긴종목") for m in out) == 26

    def test_no_good_news_no_message_and_order_in_the_briefing(self) -> None:
        assert briefing_service.good_news_messages([_row(1, "가", 70)]) == []
        rows = [
            {
                **_row(1, "가", 70),
                "technical_score": 70.0,
                "list_reasons": ["POSITIVE_NEWS_OVERLAY"],
            }
        ]
        out = briefing_service.build(date(2026, 10, 6), rows, {})
        assert out[1] == briefing_service.PRINCIPLE and out[2].startswith("좋은 뉴스 종목 1개")
