"""What kind of event a DART disclosure reports, read from its title. Pure.

Titles in `list.json` are standardised by DART (`자기주식취득결정`,
`단일판매ㆍ공급계약체결`, `유상증자결정`), so a table of phrases reads them
reliably. Each rule gives the event type the news readings also use, so a
disclosure and the articles about it fall into one cluster in the overlay and
count once.

**Direction is a prior where the title settles it, and absent where it does
not.** A buyback decision is good for the holder and a share issue is not,
before any detail is read; an earnings release or a merger could go either
way, and the title does not say. Those are still events — they anchor the
cluster, and their direction comes from the articles about them — but they
carry no sentiment of their own. The signed priors are assumptions, like the
overlay's half-lives, for forward testing to correct.

**Not events:** corrections (`[기재정정]` and the like) amend a filing already
counted, and result reports (`결과보고서`) close one already announced. Both
would otherwise count an event twice, days apart and outside the cluster
window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RULE_VERSION = 1

# How sure a rule is that it has named the event correctly. High: the titles
# are standard forms. It says nothing about the size of the market's reaction,
# which is what `intensity` guesses at.
RULE_CONFIDENCE = 0.8


@dataclass(frozen=True, slots=True)
class DisclosureEvent:
    event_type: str
    sentiment: float | None
    """None when the title does not settle the direction."""
    intensity: float


# Checked in order, first match wins, against the title with spaces and
# middle dots removed. Specific phrases come before the general ones they
# contain: a buyback trust being cancelled is not a buyback.
_RULES: tuple[tuple[tuple[str, ...], DisclosureEvent], ...] = (
    (("자기주식취득신탁계약해지",), DisclosureEvent("SHAREHOLDER_RETURN", -0.3, 0.3)),
    (("자기주식취득신탁계약체결",), DisclosureEvent("SHAREHOLDER_RETURN", 0.5, 0.5)),
    (("자기주식취득결정",), DisclosureEvent("SHAREHOLDER_RETURN", 0.6, 0.6)),
    (("주식소각결정",), DisclosureEvent("SHAREHOLDER_RETURN", 0.6, 0.6)),
    (("현금현물배당결정", "현금배당결정"), DisclosureEvent("SHAREHOLDER_RETURN", 0.3, 0.3)),
    (("유무상증자결정",), DisclosureEvent("CAPITAL_RAISE", -0.3, 0.5)),
    (("무상증자결정",), DisclosureEvent("SHAREHOLDER_RETURN", 0.3, 0.4)),
    (("기업가치제고계획",), DisclosureEvent("SHAREHOLDER_RETURN", 0.3, 0.3)),
    (("자기주식처분결정", "자기전환사채매도결정"), DisclosureEvent("CAPITAL_RAISE", -0.3, 0.3)),
    (("단일판매공급계약해지",), DisclosureEvent("ORDER_CONTRACT", -0.5, 0.5)),
    (("단일판매공급계약체결",), DisclosureEvent("ORDER_CONTRACT", 0.5, 0.5)),
    (("거래처와의거래중단",), DisclosureEvent("ORDER_CONTRACT", -0.5, 0.5)),
    (("유상증자결정",), DisclosureEvent("CAPITAL_RAISE", -0.5, 0.6)),
    (
        ("전환사채권발행결정", "신주인수권부사채권발행결정", "교환사채권발행결정"),
        DisclosureEvent("CAPITAL_RAISE", -0.4, 0.5),
    ),
    (("감자결정",), DisclosureEvent("CAPITAL_RAISE", -0.5, 0.5)),
    (("파산신청기각",), DisclosureEvent("LEGAL_REGULATORY", None, 0.4)),
    # The reason matters more than the form: a trading halt for a share
    # consolidation is paperwork, one for a delisting cause is not. So the
    # grave reasons are matched anywhere in the title, halt or notice alike,
    # and a halt without one is not an event at all (see `_NOT_EVENTS`).
    (
        (
            "횡령배임",
            "불성실공시법인지정",
            "상장적격성실질심사",
            "관리종목지정",
            "감사의견거절",
            "회생절차개시신청",
            "부도발생",
            "상장폐지",
        ),
        DisclosureEvent("LEGAL_REGULATORY", -0.8, 0.9),
    ),
    (("파산신청",), DisclosureEvent("LEGAL_REGULATORY", -0.6, 0.7)),
    (("소송등의제기",), DisclosureEvent("LEGAL_REGULATORY", -0.3, 0.4)),
    (("소송등의판결",), DisclosureEvent("LEGAL_REGULATORY", None, 0.4)),
    (
        (
            "회사합병결정",
            "회사분할합병결정",
            "회사분할결정",
            "주식교환이전결정",
            "영업양수결정",
            "영업양도결정",
            "타법인주식및출자증권취득결정",
            "타법인주식및출자증권처분결정",
            "유형자산양수결정",
            "유형자산양도결정",
            "유형자산취득결정",
            "유형자산처분결정",
        ),
        DisclosureEvent("MERGER_ACQUISITION", None, 0.5),
    ),
    (
        ("잠정실적", "매출액또는손익구조"),
        DisclosureEvent("EARNINGS", None, 0.5),
    ),
    (("최대주주변경", "경영권변경등에관한계약체결"), DisclosureEvent("MANAGEMENT", None, 0.4)),
    (("대표이사변경",), DisclosureEvent("MANAGEMENT", None, 0.2)),
    (("투자판단관련주요경영사항",), DisclosureEvent("OTHER", None, 0.3)),
)

# Spaces, the middle dots DART uses between nouns, and parentheses:
# `영업(잠정)실적(공정공시)` must read as containing `잠정실적`.
_SQUEEZE = re.compile(r"[\s·ㆍ‧・()]+")
_LEADING_TAG = re.compile(r"^\s*\[[^\]]*\]")


# Filings that look like events and are not: a result or status report closes
# an event already counted, a halt being lifted undoes nothing, and a halt for
# no grave reason is a technical pause.
_NOT_EVENTS = ("결과보고서", "상황보고서", "거래정지해제")
# An event at a subsidiary, reported by the listed parent, matters less to it.
_SUBSIDIARY = ("자회사의주요경영사항", "종속회사의주요경영사항")
SUBSIDIARY_WEIGHT = 0.5


def classify(report_nm: str) -> DisclosureEvent | None:
    """The event a disclosure title reports, or None if it is not one we count."""
    tag = _LEADING_TAG.match(report_nm)
    if tag is not None and "정정" in tag.group(0):
        return None
    flat = _SQUEEZE.sub("", _LEADING_TAG.sub("", report_nm))
    if any(n in flat for n in _NOT_EVENTS):
        return None
    for phrases, event in _RULES:
        if any(p in flat for p in phrases):
            if any(m in flat for m in _SUBSIDIARY):
                return DisclosureEvent(
                    event.event_type, event.sentiment, event.intensity * SUBSIDIARY_WEIGHT
                )
            return event
    return None
