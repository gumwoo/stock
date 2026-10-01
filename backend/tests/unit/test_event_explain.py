"""쉬운 설명: 9시 시가 기준 첫 1시간 길, 공시·뉴스 쉬운 말, 반응 문장, 카톡 200자, 설명 실패 격리."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.scoring import event_explain
from app.scoring.early_path import Bar, measure
from app.services import reaction_service
from app.services.briefing_service import GUIDE, MAX_CHARS, event_messages


class TestEarlyPath:
    def test_needs_the_nine_oclock_bar(self) -> None:
        assert measure([Bar(2, 100, 101, 99, 100)]) is None

    def test_the_first_bar_high_does_not_count_as_sellable(self) -> None:
        # 09:00 봉 고가 103(+3%)은 팔 수 있다고 보지 않는다. 09:03에 102.5로 처음 닿는다.
        p = measure(
            [
                Bar(0, 100, 103, 99, 101),
                Bar(1, 101, 101.5, 98, 99),
                Bar(3, 99, 102.5, 99, 102),
                Bar(30, 102, 102, 100, 100),
            ]
        )
        assert p is not None and p.hit_25 == 3 and p.hit_5 is None
        assert p.low_before_25 == pytest.approx(-2.0) and p.at_10 == pytest.approx(0.0)
        assert p.hit_25_within(10) and not p.hit_25_within(3)

    def test_the_drop_inside_the_first_bar_counts(self) -> None:
        # 시가 100에 산 뒤 09:00 봉 안에서 97까지 밀렸다가 09:02에 +2.5%.
        p = measure([Bar(0, 100, 100, 97, 99), Bar(2, 99, 103, 99, 102)])
        assert p is not None and p.hit_25 == 2 and p.low_before_25 == pytest.approx(-3.0)

    def test_a_miss_reports_the_ten_oclock_level(self) -> None:
        p = measure([Bar(0, 100, 100, 100, 100), Bar(1, 100, 101, 97, 98), Bar(59, 98, 99, 96, 97)])
        assert p is not None and p.hit_25 is None and p.at_10 == pytest.approx(-3.0)
        assert p.low_before_25 == pytest.approx(-4.0)


class TestPlain:
    def test_a_subsidiary_raise_is_softened(self) -> None:
        p = event_explain.disclosure("유상증자결정", True)
        assert p.kind == "자회사 유상증자" and p.verdict == "다소 나쁨"
        assert "자회사" in p.what and p.source == "공시 제목 규칙"

    def test_grave_listing_notices_are_bad(self) -> None:
        assert event_explain.disclosure("상장폐지", False).verdict == "나쁨"
        assert event_explain.disclosure("최대주주변경", False).verdict == "애매"

    @pytest.mark.parametrize(
        ("s", "v"),
        [(0.8, "좋음"), (0.3, "다소 좋음"), (0.1, "애매"), (-0.3, "다소 나쁨"), (-0.7, "나쁨")],
    )
    def test_news_verdicts(self, s: float, v: str) -> None:
        assert event_explain.news("ORDER_CONTRACT", s, "x").verdict == v

    def test_every_rule_phrase_has_plain_words(self) -> None:
        from app.scoring.disclosure_events import _RULES

        missing = [p for phrases, _ in _RULES for p in phrases if p not in event_explain._D]
        assert missing == []


class TestReaction:
    def test_a_subsidiary_key_is_kept_apart(self) -> None:
        assert reaction_service.disclosure_key("유상증자결정(종속회사의주요경영사항)") == (
            "유상증자결정|자회사",
            "CAPITAL_RAISE|down",
        )
        assert reaction_service.disclosure_key("[기재정정]유상증자결정") is None

    def test_the_sentence_is_written_as_a_record(self) -> None:
        s = reaction_service.Stat(
            n=98,
            hit_10=0.41,
            hit_60=0.54,
            hit_minute=3,
            low_before=-1.13,
            miss_at_10=-4.33,
            hit5_60=0.2,
            gap_median=-0.8,
        )
        text = reaction_service.fmt(s, "지난 3개월 같은 공시")
        assert "98건" in text and "54%" in text and "-4.3%" in text and "사세요" not in text
        assert reaction_service.fmt(None, "x") == "기록 부족"

    def test_the_reference_table_is_in_the_repository(self) -> None:
        stat, scope = reaction_service.disclosure_stat("유상증자결정")
        assert stat is not None and stat.n >= reaction_service.MIN_N and scope == "같은 공시"


def test_kakao_event_messages_fit() -> None:
    e: dict[str, Any] = {
        "kind": "자회사 유상증자",
        "verdict": "다소 나쁨",
        "what": "아주 긴 설명 " * 30,
        "why": "이유 " * 40,
        "usual_short": "3개월 같은 공시 98건: 9시 시가 기준 1시간 내 +2.5% 54%(보통 3분)·닿기 전 평균 -1.1%·못 닿으면 10시 평균 -4.3%",
        "usual_ours": "우리 목록 기록 " * 10,
        "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260929000646",
    }
    out = event_messages("한화솔루션", [e, {**e, "kind": "공급 계약"}, {**e, "kind": "배당"}])
    assert all(len(m) <= MAX_CHARS for m in out) and len(GUIDE) <= MAX_CHARS
    assert sum(m.startswith("📌") for m in out) == 2  # 종목당 사건 둘까지
    assert any("보통:" in m for m in out)


def test_a_failure_to_explain_leaves_the_list_as_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.realtime import gateway

    member = gateway.LiveMember(
        instrument_id=1,
        code="009830",
        name="한화솔루션",
        rank=1,
        reasons=("DISCLOSURE_EVENT",),
        overlay_points=None,
        attention_surge=None,
        regime=None,
    )

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(gateway.event_brief_service, "explain", boom)
    asof = datetime(2026, 9, 30, 23, 38, tzinfo=UTC)
    assert gateway.attach_explanations([member], date(2026, 10, 1), asof) == [member]


def test_kakao_keeps_the_reason_for_a_subsidiary_disclosure() -> None:
    p = event_explain.disclosure("유상증자결정", True)
    stat, scope = reaction_service.disclosure_stat("유상증자결정(종속회사의주요경영사항)")
    usual = reaction_service.fmt(stat, f"3개월 {scope}", short=True)
    e = {"kind": p.kind, "verdict": p.verdict, "what": p.what, "why": p.why, "usual_short": usual}
    (text,) = event_messages("에코프로비엠", [e])
    assert len(text) <= MAX_CHARS and "왜: 주식 수가 늘어" in text and "보통: 3개월" in text


def test_kakao_news_keeps_the_list_record_as_usual() -> None:
    # 뉴스는 3개월 기준표가 없어 우리 목록 기록이 "보통"이다. 무슨 일이 길어도 빠지지 않는다.
    s = reaction_service.Stat(
        n=12, hit_10=0.3, hit_60=0.5, hit_minute=4, low_before=-1.2, miss_at_10=-2.1, hit5_60=0.1
    )
    e = {
        "kind": "수주·계약",
        "verdict": "좋음",
        "what": "가" * 120,
        "why": "새 계약·수주로 매출이 늘 수 있음",
        "usual_ours": reaction_service.fmt(s, "우리 목록"),
        "usual_ours_short": reaction_service.fmt(s, "우리 목록", short=True),
    }
    out = event_messages("HLB바이오스텝", [e, dict(e)])
    assert len(out) == 1  # 같은 종류·판단은 한 번
    assert "보통: 우리 목록 12건" in out[0] and "왜:" in out[0] and len(out[0]) <= MAX_CHARS
