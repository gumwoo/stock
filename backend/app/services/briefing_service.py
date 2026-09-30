"""아침 목록 브리핑 문장(카카오톡 나에게 보내기용). 순수 — 이미 만든 신호 행과 관찰 목록 행(dict)만 받는다.

카카오 도구는 메시지당 200자까지라, 머리말(순위) + 종목마다 점수·근거 한 개 + 뉴스·공시 한두 개로 나누고, 짧은 것은
200자 안에서 이어 붙인다. 점수는 08:40 저장값 그대로이고 새로 계산하지 않는다. 순위:

- 기술 1~3: 기술 점수 높은 순
- 재무 1~3: 재무 점수 높은 순(재무를 쓰지 못한 종목 제외)
- 종합 1~3: 합계(기술 60% + 재무 40%, 재무가 없으면 그만큼 낮다) 높은 순

판단 보류·채점 상세 없음은 모든 순위에서 뺀다. 동점은 목록 순위. 사실만 적고 매매를 권하지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

MAX_CHARS = 200
TOP = 3

ACTION = {"BUY_INTEREST": "매수 관심", "WATCH": "관망", "CAUTION": "주의", "ABSTAINED": "판단 보류"}
REASON = {
    "DISCOVERY_SURGE": "뉴스 급증",
    "POSITIVE_NEWS_OVERLAY": "좋은 뉴스",
    "NEGATIVE_NEWS_OVERLAY": "나쁜 뉴스",
    "DISCLOSURE_EVENT": "공시",
    "SEARCH_SURGE": "검색 급증",
}
EVENT = {
    "PRICE_MOVE": "주가",
    "OTHER": "기타",
    "INDUSTRY": "업황",
    "PRODUCT": "제품",
    "MANAGEMENT": "경영",
    "ANALYST_RATING": "증권사 의견",
    "EARNINGS": "실적",
    "GUIDANCE": "실적 전망",
    "SHAREHOLDER_RETURN": "주주환원",
    "ORDER_CONTRACT": "수주·계약",
    "CAPITAL_RAISE": "자금 조달",
    "LEGAL_REGULATORY": "법·규제",
    "MERGER_ACQUISITION": "인수합병",
}
LIMIT = {"LOCKED": "전일 점상한가", "CLOSED": "전일 상한가", "TOUCHED": "전일 상한가 터치"}
OPINION = {"BUY": "매수", "HOLD": "중립", "SELL": "매도"}
NEWS_PER_NAME = 2


def clip(text: str, limit: int) -> str:
    """공백을 하나로 줄이고(DART 제목에 공백이 몰려 있다) 넘치면 말줄임."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _fundamental_missing(row: Mapping[str, Any]) -> bool:
    factors = (row.get("detail") or {}).get("factors") or []
    f = next((x for x in factors if x.get("engine") == "FUNDAMENTAL"), None)
    return f is None or f.get("availability") == "UNAVAILABLE" or not f.get("effective_weight")


def _eligible(row: Mapping[str, Any]) -> bool:
    return (
        row.get("detail") is not None
        and row.get("action") not in (None, "ABSTAINED")
        and row.get("total_score") is not None
        and bool(row.get("weight_total"))
    )


def weight_total(detail: Any) -> float | None:
    """채점 상세의 참여 가중치 합(요인 effective_weight 합). 상세가 없으면 None. 신호 탭과 list-review가 같이 쓴다."""
    factors = detail.get("factors") if isinstance(detail, dict) else None
    if not isinstance(factors, list):
        return None
    return sum(float(f.get("effective_weight") or 0.0) for f in factors if isinstance(f, dict))


def judged(row: Mapping[str, Any]) -> float:
    return float(row["total_score"]) / float(row["weight_total"])


def rankings(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    ok = [r for r in rows if _eligible(r)]

    def top(key: str, pool: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        have = [r for r in pool if r.get(key) is not None]
        return sorted(have, key=lambda r: (-float(r[key]), r["rank"]))[:TOP]

    return {
        "기술": top("technical_score", ok),
        "재무": top("fundamental_score", [r for r in ok if not _fundamental_missing(r)]),
        "종합": top("total_score", ok),
    }


def _won(n: float) -> str:
    return f"{round(n):,}원"


def _day(iso: str) -> str:
    _, m, d = iso.split("-")
    return f"{int(m)}/{int(d)}"


def analyst_variants(a: Mapping[str, Any] | None) -> list[str]:
    """증권사 줄, 긴 것부터 짧은 것까지. 조회하지 못했으면 없음, 잘려서 0건이면 없음."""
    if not a:
        return []
    if a.get("count", 0) == 0:
        return [] if a.get("truncated") else ["증권사(참고): 3개월 리포트 없음"]
    count = f"{a['count']}{'건 이상' if a.get('truncated') else '건'}({a['brokers']}곳)"
    head = f"증권사(참고): 3개월 {count}"
    target = ""
    if a.get("avg_target") is not None:
        label = "평균 목표가" if a.get("target_brokers", 0) > 1 else "목표가"
        up = a.get("upside_pct")
        target = f"·{label} {_won(a['avg_target'])}" + (
            f"(종가 대비 {'+' if up > 0 else ''}{up:.1f}%)" if up is not None else ""
        )
    latest = ""
    lt = a.get("latest")
    if lt:
        raw = "".join(str(lt["opinion"]).lower().split())
        op = OPINION.get(lt["label"]) or (
            "의견 없음" if raw in ("notrated", "nr") else lt["opinion"]
        )
        tp = f" {_won(lt['target'])}" if lt.get("target") is not None else ""
        latest = f"·최근 {_day(lt['date'])} {lt['broker']} {op}{tp}"
    moves = (
        f"·목표가 상향{a['raised']}/하향{a['lowered']}"
        if a.get("raised") or a.get("lowered")
        else ""
    )
    return [head + target + latest + moves, head + target + latest, head + target, head]


def score_message(row: Mapping[str, Any], tags: Sequence[str], has_news: bool) -> str:
    missing = _fundamental_missing(row)
    fund = "없음" if missing else f"{float(row['fundamental_score']):.1f}"
    # 재무가 없으면 판단은 기술 점수로, 합계는 기술 몫(60)까지만 된다.
    cap = f"(최대 {round(float(row['weight_total']) * 100)})" if missing else ""
    tech = row.get("technical_score")
    lines = [
        clip(f"■ {row['name']} {row.get('code') or ''} [{', '.join(tags)}]", 60),
        f"판단 {judged(row):.1f} {ACTION.get(row['action'], row['action'])}",
        f"기술 {float(tech):.1f}·재무 {fund}·합계 {float(row['total_score']):.1f}{cap}"
        if tech is not None
        else f"재무 {fund}·합계 {float(row['total_score']):.1f}",
        clip("이유: " + ", ".join(REASON.get(x, x) for x in row.get("list_reasons") or []), 60),
    ]
    marks = [
        m
        for m in (
            "지수 대형주" if row.get("heavyweight") else None,
            LIMIT.get(row.get("prev_limit") or ""),
        )
        if m
    ]
    if marks:
        lines.append(" · ".join(marks))
    if not has_news:
        lines.append("뉴스·공시 묶음 없음")
    base = "\n".join(lines)
    for variant in analyst_variants(row.get("analyst")):
        text = base + "\n" + variant
        if len(text) <= MAX_CHARS:
            return text
    return clip(base, MAX_CHARS) if len(base) > MAX_CHARS else base


def news_messages(name: str, events: Sequence[Mapping[str, Any]]) -> list[str]:
    """목록 이유 뒤 뉴스·공시 묶음(뉴스 점수 큰 순) 앞 두 개. 제목과 링크, 200자에 맞게."""
    out: list[str] = []
    head = f"{name} 뉴스·공시"
    current = head
    for i, e in enumerate(events[:NEWS_PER_NAME], 1):
        url = str(e.get("url") or "")
        if len(url) > 150:  # 잘린 링크는 열리지 않는다
            url = ""
        title = " ".join(str(e.get("title") or "").split())
        # 공시 제목은 이미 "[공시]"로 시작한다. 그때는 분류 이름을 덧붙이지 않는다.
        tag = "" if title.startswith("[") else f"[{EVENT.get(str(e.get('event_type')), '뉴스')}] "
        prefix = f"{i}) {tag}"
        room = MAX_CHARS - len(head) - 1 - len(prefix) - (len(url) + 1 if url else 0)
        block = prefix + clip(title, max(20, room)) + (f"\n{url}" if url else "")
        candidate = f"{current}\n{block}"
        if len(candidate) <= MAX_CHARS:
            current = candidate
            continue
        if current != head:
            out.append(current)
        current = f"{head}\n{block}"[:MAX_CHARS]
    if current != head:
        out.append(current)
    return out


def pack(parts: Sequence[str]) -> list[str]:
    """이웃한 짧은 메시지를 200자 안에서 합친다. 각 조각은 이미 200자 이하."""
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) + 2 + len(p) <= MAX_CHARS:
            out[-1] = f"{out[-1]}\n\n{p}"
        else:
            out.append(p)
    return out


def build(
    day: date,
    rows: Sequence[Mapping[str, Any]],
    events_by_id: Mapping[int, Sequence[Mapping[str, Any]]],
) -> list[str]:
    ranks = rankings(rows)
    title = f"[{day.month}/{day.day} 아침 목록 점수 순위] {len(rows)}종목·08:40 채점·참고용(매매 권유 아님)"
    rank_lines = []
    for label, top in ranks.items():
        key = {"기술": "technical_score", "재무": "fundamental_score", "종합": "total_score"}[label]
        items = ", ".join(f"{clip(r['name'], 10)} {float(r[key]):.1f}" for r in top) or "해당 없음"
        rank_lines.append(f"{label}{'(합계)' if label == '종합' else ''}: {items}")
    header = [title, *rank_lines]
    parts: list[str] = []
    text = "\n".join(header)
    if len(text) <= MAX_CHARS:
        parts.append(text)
    else:
        parts += [clip(line, MAX_CHARS) for line in header]

    tags: dict[int, list[str]] = {}
    order: list[Mapping[str, Any]] = []
    for label, top in ranks.items():
        for n, r in enumerate(top, 1):
            i = int(r["instrument_id"])
            if i not in tags:
                tags[i] = []
                order.append(r)
            tags[i].append(f"{label}{n}")
    # 합치기는 한 종목 안에서만: 앞 종목의 뉴스와 다음 종목의 점수가 한 메시지에 섞이지 않게.
    messages = list(parts)
    for r in order:
        i = int(r["instrument_id"])
        events = list(events_by_id.get(i) or [])
        messages += pack(
            [score_message(r, tags[i], bool(events)), *news_messages(str(r["name"]), events)]
        )
    return messages
