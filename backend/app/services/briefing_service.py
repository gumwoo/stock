"""아침 목록 브리핑 문장(카카오톡 나에게 보내기용). 순수 — 이미 만든 신호 행과 관찰 목록 행(dict)만 받는다.

카카오 도구는 메시지당 200자까지라, 머리말(순위) + 종목마다 점수·근거 한 개 + 뉴스·공시 한두 개로 나누고, 짧은 것은
200자 안에서 이어 붙인다. 점수는 아침 채점(2026-10-02부터 08:35, 그 전 08:40) 저장값 그대로이고 새로 계산하지 않는다. 순위:

- 기술 1~3: 기술 점수 높은 순
- 재무 1~3: 재무 점수 높은 순(재무를 쓰지 못한 종목 제외)
- 종합 1~3: 합계(기술 60% + 재무 40%, 재무가 없으면 그만큼 낮다) 높은 순

판단 보류·채점 상세 없음은 모든 순위에서 뺀다. 동점은 목록 순위. 사실만 적고 매매를 권하지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

MAX_CHARS = 200
NL = "\n"
SEOUL = ZoneInfo("Asia/Seoul")
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
GUIDE = (
    "※ 읽는 법: 좋은 일/나쁜 일은 공시는 제목 규칙, 뉴스는 AI 판독. '보통'은 9시 시가에 샀다면 1시간 안에 +2.5%까지 간 비율"
    "(10분 안 비율·보통 걸린 분), 닿기 전 최저 평균, 못 닿았을 때 10시 평균이다. 지난 기록일 뿐 그대로 된다는 뜻이 아니다."
)


# 소유자가 직접 정한 매매 원칙(2026-10-06). 시스템의 권유가 아니다. 숫자는 3개월 공시 표본(9시 시가 매수, 비용 0.30%,
# 09:05 전에 +2.5%에 닿은 날)의 지난 기록이다: 평균은 비슷하고, 들고 있으면 더 나빴던 날이 절반을 넘는다.
PRINCIPLE = (
    "[내 매매 원칙·직접 정한 것] 목표 +2.5~5%에 닿으면 바로 판다(지정가 미리). 3개월 공시 표본(9시 시가 매수) 09:05 전 +2.5% 도달 497건: "
    "바로 +2.26%·10시 보유 +2.31%로 평균 비슷, 보유가 더 나쁜 날 53%. 좋은 공시 144건은 바로 +2.26%·보유 +2.04%. "
    "참고용(매매 권유 아님)"
)
GOOD_NEWS = "POSITIVE_NEWS_OVERLAY"
GOOD_NEWS_NOTE = (
    "참고: 선정 3 전 5일 기록(좋은 뉴스 67건)에서 결과와 같은 방향인 건 기술 점수(상관 +0.25)였고 재무는 아니었다"
    "(-0.04). 표본이 작아 근거는 약하다"
)


def good_news_messages(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """좋은 뉴스로 목록에 든 종목을 기술 점수 높은 순으로(표시만 — 선정·점수 가중치는 그대로). 점수 없음은 맨 뒤 "점수 없음"."""
    good = [r for r in rows if GOOD_NEWS in (r.get("list_reasons") or [])]
    if not good:
        return []

    def score(r: Mapping[str, Any]) -> float | None:
        v = r.get("technical_score")
        return None if v is None else float(v)

    good.sort(key=lambda r: (score(r) is None, -(score(r) or 0.0), r.get("rank") or 0))
    items = [
        f"{clip(str(r['name']), 10)} {'점수 없음' if score(r) is None else f'{score(r):.1f}'}"
        for r in good
    ]
    out: list[str] = []
    current = f"좋은 뉴스 종목 {len(good)}개(기술 점수 순): " + items[0]
    for item in items[1:]:
        if len(current) + 2 + len(item) > MAX_CHARS:
            out.append(current)
            current = "좋은 뉴스(이어서): " + item
        else:
            current = f"{current}, {item}"
    out.append(current)
    return [
        *out[:-1],
        *pack([out[-1], GOOD_NEWS_NOTE]),
    ]  # 마지막 조각과 주의 줄은 들어가면 한 통으로


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


def event_messages(name: str, events: Sequence[Mapping[str, Any]]) -> list[str]:
    """사건마다 쉬운 설명 한 통(무슨 일 · 좋은 일? · 왜 · 보통)과 원문 링크 한 줄. 설명이 없으면 예전처럼 제목과 링크.

    같은 종류·판단의 사건(예: 소송 판결 두 건)은 한 번만 보낸다 — 종목당 두 칸이 같은 문장으로 차지 않게.
    """
    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    picked: list[Mapping[str, Any]] = []
    for e in events:
        if e.get("kind"):
            key = (str(e["kind"]), str(e.get("verdict") or ""))
            if key in seen:
                continue
            seen.add(key)
        picked.append(e)
    for e in picked[:NEWS_PER_NAME]:
        if not e.get("kind"):
            out += news_messages(name, [e])
            continue
        head = f"📌 {name} · {e['kind']} — {e.get('verdict') or '애매'}"
        what = f"무슨 일: {e.get('what') or e.get('title') or ''}"
        why = f"왜: {e.get('why') or ''}"
        if e.get("usual_short"):
            usual = f"보통: {e['usual_short']}"
            ours = f"우리 목록: {e['usual_ours_short']}" if e.get("usual_ours_short") else ""
        else:
            # 뉴스는 3개월 기준표가 없다: 우리 목록 기록이 "보통"이다(빼지 않는다)
            usual = f"보통: {e.get('usual_ours_short') or '기록 부족'}"
            ours = ""
        out.append(clip_lines(fit_event(head, what, why, usual, ours)))
        url = str(e.get("url") or "")
        if url and len(url) <= 150:
            line = f"{name} 원문: {url}"
            out.append(line if len(line) <= MAX_CHARS else url)
    return out


def fit_event(head: str, what: str, why: str, usual: str, ours: str) -> str:
    """사건 한 통을 200자에 맞춘다. 남는 자리는 "왜"(좋은 일/나쁜 일의 이유)에 먼저 주고, "무슨 일"은 40자까지 줄인다.
    "우리 목록"은 넣으면 둘 중 하나가 잘릴 때 뺀다."""
    what, why = clip(what, 10_000), clip(why, 10_000)
    for extra in ([ours] if ours else [], []):
        room = (
            MAX_CHARS - len(NL.join(x for x in (head, usual, *extra) if x)) - 2
        )  # 무슨 일·왜 줄바꿈
        if len(what) + len(why) <= room:
            return NL.join(x for x in (head, what, why, usual, *extra) if x)
        if extra:
            continue
        why_n = max(1, min(len(why), max(room - 40, room // 2)))
        what_n = max(1, room - why_n)
        return NL.join(x for x in (head, clip(what, what_n), clip(why, why_n), usual) if x)
    raise AssertionError("unreachable")


def clip_lines(text: str) -> str:
    """줄바꿈을 지키며 200자로 자른다."""
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


def pack(parts: Sequence[str]) -> list[str]:
    """이웃한 짧은 메시지를 200자 안에서 합친다. 각 조각은 이미 200자 이하."""
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) + 2 + len(p) <= MAX_CHARS:
            out[-1] = f"{out[-1]}\n\n{p}"
        else:
            out.append(p)
    return out


def scored_at(rows: Sequence[Mapping[str, Any]]) -> str:
    """머리말의 채점 시각: 행들의 `evaluated_at` 중 가장 늦은 것(서울 HH:MM) + "·". 없으면 빈 문자열."""
    times = [str(r["evaluated_at"]) for r in rows if r.get("evaluated_at")]
    if not times:
        return ""
    latest = max(datetime.fromisoformat(t) for t in times).astimezone(SEOUL)
    return f"{latest:%H:%M} "


def build(
    day: date,
    rows: Sequence[Mapping[str, Any]],
    events_by_id: Mapping[int, Sequence[Mapping[str, Any]]],
    excluded: int = 0,
    gap_up: int = 0,
) -> list[str]:
    """`excluded`: 선정 3에서 뺀 종목 수(판단 점수 40 미만·전일 +15% 이상). `gap_up`: 08:50 예상 갭 판정으로 뺀 수(브리핑이
    늦게 돌아 판정 뒤에 만들어질 때만 0이 아니다). 머리말에 한 번 적는다."""
    ranks = rankings(rows)
    when = scored_at(rows)
    scored = f"{when}채점" if when else "채점 기록 없음"
    out = f"(점수 40 미만·전일 급등 {excluded}개 뺌)" if excluded else ""
    out += f"(갭 +3% {gap_up}개 뺌)" if gap_up else ""
    title = f"[{day.month}/{day.day} 아침 목록 점수 순위] {len(rows)}종목{out}·{scored}·참고용(매매 권유 아님)"
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
        parts += pack(
            [clip(line, MAX_CHARS) for line in header]
        )  # 넘치면 줄 단위로 나눠 최대한 합친다
    # 순서: 머리말 → 내 매매 원칙 → 좋은 뉴스(기술 점수 순) → 읽는 법(종목별 메시지 바로 앞) → 종목별.
    parts.append(PRINCIPLE)
    parts += good_news_messages(rows)
    if any(e.get("kind") for es in events_by_id.values() for e in es):
        parts.append(GUIDE)

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
            [score_message(r, tags[i], bool(events)), *event_messages(str(r["name"]), events)]
        )
    return messages


def gap_messages(
    day: date,
    check: Mapping[str, Any],
    gapped: Sequence[tuple[str, float | None]],
    before: Mapping[str, Sequence[Mapping[str, Any]]],
    after: Mapping[str, Sequence[Mapping[str, Any]]],
    remaining: int,
) -> list[str]:
    """9시 전 예상 갭 판정 결과(카톡). `gapped`: (종목명, 예상 갭 %) — 판정으로 뺀 종목. `before`/`after`: 뺀 앞뒤의 점수 순위."""
    at = datetime.fromisoformat(str(check["at"])).astimezone(SEOUL)
    head = f"[{day.month}/{day.day} 9시 전 예상 갭] {at:%H:%M} 판정·참고용(매매 권유 아님)"
    if gapped:
        line = f"예상 시가 +3% 이상 {len(gapped)}개를 목록에서 뺌 · 남은 {remaining}종목"
    else:
        line = f"예상 시가 +3% 이상 종목 없음 · 남은 {remaining}종목"
    counts = (
        f"조회 {check.get('checked', 0)}·판정 {check.get('judgeable', 0)}·"
        f"불일치 {check.get('mismatched', 0)}·실패 {check.get('failed', 0)}"
    )
    lines = [head, line, counts]
    asked = int(check.get("checked") or 0) + int(check.get("failed") or 0)
    if asked and (asked - int(check.get("judgeable") or 0)) * 2 > asked:
        lines.append(
            "⚠ 판정하지 못한 종목이 절반을 넘습니다(실패·불일치·값 없음 — 갭업 종목이 남아 있을 수 있음)"
        )
    parts = [clip_lines("\n".join(lines))]
    if gapped:
        names = [clip(n, 10) if p is None else f"{clip(n, 10)} {p:+.1f}%" for n, p in gapped]
        current = "뺀 종목: " + names[0]
        for item in names[1:]:
            if len(current) + 2 + len(item) > MAX_CHARS:
                parts.append(current)
                current = "뺀 종목(이어서): " + item
            else:
                current = f"{current}, {item}"
        parts.append(current)
    changed = [
        label
        for label in after
        if [r["instrument_id"] for r in before.get(label, [])]
        != [r["instrument_id"] for r in after[label]]
    ]
    if changed:
        rank_lines = ["08:44 순위에서 바뀜(뺀 종목 제외하고 다시 매김):"]
        for label in changed:
            key = {"기술": "technical_score", "재무": "fundamental_score", "종합": "total_score"}[
                label
            ]
            items = (
                ", ".join(f"{clip(r['name'], 10)} {float(r[key]):.1f}" for r in after[label])
                or "해당 없음"
            )
            rank_lines.append(f"{label}: {items}")
        parts.append(clip_lines("\n".join(rank_lines)))
    return pack(parts)


def gap_warning(day: date, why: str) -> list[str]:
    return [
        f"[{day.month}/{day.day} 9시 전 예상 갭] 판정을 하지 못했습니다({why}). 예상 시가 +3% 이상 종목이 목록에 남아 있을 수 있습니다"
    ]
