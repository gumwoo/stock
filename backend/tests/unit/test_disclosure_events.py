"""Disclosure titles read as events. The titles are real ones from DART, September 2026."""

from __future__ import annotations

import pytest

from app.scoring.disclosure_events import SUBSIDIARY_WEIGHT, classify


@pytest.mark.parametrize(
    ("title", "event_type", "sentiment"),
    [
        ("주요사항보고서(자기주식취득결정)", "SHAREHOLDER_RETURN", 0.6),
        ("주식소각결정", "SHAREHOLDER_RETURN", 0.6),
        ("주요사항보고서(자기주식취득신탁계약해지결정)", "SHAREHOLDER_RETURN", -0.3),
        ("단일판매ㆍ공급계약체결", "ORDER_CONTRACT", 0.5),
        ("주요사항보고서(유상증자결정)", "CAPITAL_RAISE", -0.5),
        ("주요사항보고서(전환사채권발행결정)", "CAPITAL_RAISE", -0.4),
        ("주요사항보고서(자기전환사채매도결정)", "CAPITAL_RAISE", -0.3),
        ("유무상증자결정(종속회사의주요경영사항)", "CAPITAL_RAISE", -0.3),
        ("주권매매거래정지              (상장폐지 사유발생)", "LEGAL_REGULATORY", -0.8),
        (
            "기타시장안내(관리종목지정우려종목)              (주가 1,000원 미달)",
            "LEGAL_REGULATORY",
            -0.8,
        ),
        ("주요사항보고서(회생절차개시신청)", "LEGAL_REGULATORY", -0.8),
        ("소송등의제기ㆍ신청(일정금액이상의청구)", "LEGAL_REGULATORY", -0.3),
        ("연결재무제표기준영업(잠정)실적(공정공시)", "EARNINGS", None),
        ("타법인주식및출자증권취득결정", "MERGER_ACQUISITION", None),
        ("최대주주변경", "MANAGEMENT", None),
    ],
)
def test_real_titles(title: str, event_type: str, sentiment: float | None) -> None:
    event = classify(title)
    assert event is not None, title
    assert (event.event_type, event.sentiment) == (event_type, sentiment)


@pytest.mark.parametrize(
    "title",
    [
        # A halt lifted, and a halt for a share consolidation: nothing happened.
        "주권매매거래정지해제              (액면병합 주권 변경상장)",
        # Lifted, even when the reason it was imposed is a grave one.
        "주권매매거래정지해제              (관리종목지정 해제)",
        "주권매매거래정지              (주식의 병합, 분할 등 전자등록 변경, 말소)",
        # Corrections amend an event already counted.
        "[기재정정]단일판매ㆍ공급계약체결",
        "[첨부정정]주요사항보고서(유상증자결정)",
        # Results close an event already counted.
        "유상증자또는주식관련사채등의발행결과",
        "자기주식취득결과보고서",
        # Not events at all.
        "주주총회소집결의",
        "최대주주등소유주식변동신고서",
        "기업설명회개최",
    ],
)
def test_not_events(title: str) -> None:
    assert classify(title) is None, title


def test_a_subsidiarys_event_weighs_less() -> None:
    parent = classify("단일판매ㆍ공급계약체결")
    child = classify("단일판매ㆍ공급계약체결(자회사의 주요경영사항)")
    assert parent is not None and child is not None
    assert child.intensity < parent.intensity
    assert child.intensity == pytest.approx(parent.intensity * SUBSIDIARY_WEIGHT)
    assert SUBSIDIARY_WEIGHT < 1
    assert child.sentiment == parent.sentiment
