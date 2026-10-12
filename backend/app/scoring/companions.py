"""장 시작 직후 동반주(순수): 등락률 순위 읽기, 이름 묶음 규칙, AI 관련주 이름 확인, 알림 고르기.

09:01~09:10에 크게 오르는 종목(이끄는 종목)과 시장에서 함께 묶이는 종목(동반 후보)을 오늘의 관찰에 보여 준다(2026-10-12 소유자 요청).
표시·기록 전용이다 — 목록·점수·카톡 순위에는 쓰지 않고, 매매 권유가 아니다.

관계 출처는 둘이다.
- **이름(NAME)**: 종목명 앞 한글 두 글자가 같은 종목 2~6개 묶음(예: 금호석유화학·금호에이치티·금호건설·금호전기·금호타이어). 시장이
  이름으로 묶어 움직이는 일이 잦아서 기본으로 쓴다(2026-10-12 금호전기·금호건설, 형지I&C·형지글로벌). 흔한 앞글자(한국·에스·케이 …)와
  큰 그룹(묶음이 6개 넘음)은 쓰지 않는다. 관계 없는 회사가 같이 묶일 수 있어 화면에 "이름 같음"으로 낮게 보인다.
- **AI(추정)**: 밤에 Claude에게 물은 관련주(`companion_service`). 원형(2026-10-12)에서 종목코드는 대부분 틀렸고, 같은 질문에 넣은 다른
  종목을 고르는 경향과 틀린 이유가 있었다. 그래서 이름만 받아 마스터와 정확히 하나가 맞을 때만 쓰고, 같은 질문 묶음 안에서 고른 관계는
  이름 규칙도 같은 관계를 낼 때만 보인다.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

LEADER_AT = 7.0
"""이끄는 종목으로 보는 전일 대비 등락률(%)."""
MAX_COMPANIONS = 6
"""이끄는 종목 하나에 보이는 동반 후보 수."""
NAME_GROUP = (2, 6)
"""이름 묶음으로 쓰는 묶음 크기(포함)."""
NAME_RULE_VERSION = 1
WINDOW = (timedelta(minutes=1), timedelta(minutes=10))
"""개장 뒤 이 구간에서만 순위를 받는다(연초처럼 10시에 여는 날도 개장 시각 기준)."""

# 2026-10-12 KR 보통주 2,648개 분포에서: 묶음이 커서 이미 빠지는 것(한국 50·에스 45 …) 말고도, 서로 관계없는 회사가 흔히 쓰는
# 앞글자. 판단은 사람 손이다 — 틀린 묶음을 보면 여기에 더한다.
STOP_PREFIXES = frozenset(
    {
        "한국", "대한", "에스", "케이", "아이", "에이", "제이", "와이", "바이", "하이", "유니", "네오", "에코", "이노",
        "코스", "이지", "디와", "파인", "나노", "오리", "넥스", "폴라", "미래", "서울", "우리", "신한", "하나", "동일",
        "제일", "대성", "동양", "세원", "대원", "한일", "유진", "동국", "삼양", "원익", "한솔", "디에", "엘에", "에프",
        "티에", "피에", "비에", "씨에", "엠에", "지에", "알에", "오에",
        # 영문 머리글자를 읽은 이름(2026-10-12 원형: 브이씨 → 브이티·브이엠·브이원텍)
        "브이", "디엔", "엔에", "엘비", "엠씨", "씨앤", "에이치", "아이씨",
        # 지역명(원형: 경남제약 → 경남스틸)
        "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북",
        "전남", "경북", "경남", "제주", "경인", "호남", "영남", "동부", "서부", "남부", "북부",
    }
)  # fmt: skip
NOT_STOCKS = ("스팩", "리츠")


def normalize(name: str) -> str:
    """이름 비교용: (주)·주식회사·공백·괄호를 지우고 영문은 대문자."""
    return re.sub(r"\(주\)|주식회사|[\s()\uff08\uff09]", "", name or "").upper()  # 전각 괄호 포함


def name_prefix(name: str) -> str | None:
    if any(w in name for w in NOT_STOCKS):
        return None
    m = re.match(r"^[가-힣]{2}", name)
    if m is None or m.group(0) in STOP_PREFIXES:
        return None
    return m.group(0)


def name_groups(names: Mapping[int, str]) -> dict[int, list[int]]:
    """종목 id → 같은 이름 묶음의 다른 종목 id들(묶음 크기 2~6만)."""
    groups: dict[str, list[int]] = defaultdict(list)
    for i, n in names.items():
        p = name_prefix(n)
        if p is not None:
            groups[p].append(i)
    out: dict[int, list[int]] = {}
    lo, hi = NAME_GROUP
    for members in groups.values():
        if lo <= len(members) <= hi:
            for i in members:
                out[i] = sorted(j for j in members if j != i)
    return out


# --- 등락률 순위 --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mover:
    code: str
    name: str
    change_pct: float
    price: float | None
    open_change_pct: float | None
    volume: int | None


def _num(raw: Any) -> float | None:
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def common_share(code: str) -> bool:
    """보통주인가: 6자리 코드 끝자리가 0. 우선주는 5·7·9 등(실측: 금호건설우 002995), 새 영문 섞인 코드도 끝이 0이면 보통주."""
    return len(code) == 6 and code.endswith("0")


def parse_ranking(rows: Iterable[Mapping[str, Any]]) -> list[Mover]:
    """KIS 등락률 순위(FHPST01700000, 종가 대비) 행 → 보통주만, 등락률 큰 순."""
    out = []
    for r in rows:
        code = str(r.get("stck_shrn_iscd") or "").strip()
        change = _num(r.get("prdy_ctrt"))
        if not common_share(code) or change is None:
            continue
        vol = _num(r.get("acml_vol"))
        out.append(
            Mover(
                code=code,
                name=str(r.get("hts_kor_isnm") or "").strip(),
                change_pct=change,
                price=_num(r.get("stck_prpr")),
                open_change_pct=_num(r.get("oprc_vrss_prpr_rate")),
                volume=int(vol) if vol is not None else None,
            )
        )
    out.sort(key=lambda m: -m.change_pct)
    return out


def traded(movers: Sequence[Mover]) -> bool:
    """체결이 있었나. 개장 전(예: 수능일처럼 늦게 여는 날을 달력이 모를 때)에는 순위 거래량이 모두 0이다."""
    return any((m.volume or 0) > 0 for m in movers)


def in_window(now: datetime, open_at: datetime) -> bool:
    """분 단위로 내려 비교한다 — 09:10:00에 발화한 잡이 잠금을 잡느라 09:10:00.05에 시작해도 그 분을 센다."""
    minute = now.replace(second=0, microsecond=0)
    return open_at + WINDOW[0] <= minute <= open_at + WINDOW[1]


# --- AI 관련주 이름 확인 ------------------------------------------------------------------------------------------


def resolve_names(answer: Iterable[str], master: Mapping[str, Sequence[int]]) -> list[int | None]:
    """AI가 댄 이름 → 종목 id(정규화한 이름이 마스터에서 정확히 하나일 때만). `master`: 정규화 이름 → id들."""
    out: list[int | None] = []
    for n in answer:
        ids = master.get(normalize(n), ())
        out.append(ids[0] if len(ids) == 1 else None)
    return out


# --- 알림 ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Related:
    instrument_id: int
    sources: tuple[str, ...]
    """NAME / AI 중 하나 이상."""
    reason: str | None = None


def merge(
    leader: int,
    by_name: Mapping[int, Sequence[int]],
    by_ai: Mapping[int, Sequence[tuple[int, str, bool]]],
) -> list[Related]:
    """이끄는 종목의 관계: 이름 묶음 + AI(관련 id, 이유, 같은 질문 묶음 안에서 골랐나). 묶음 안 AI 관계는 이름 규칙이 같이 낼 때만."""
    names = set(by_name.get(leader, ()))
    found: dict[int, dict[str, Any]] = {i: {"sources": ["NAME"], "reason": None} for i in names}
    for rid, reason, in_batch in by_ai.get(leader, ()):
        if rid == leader or (in_batch and rid not in names):
            continue
        entry = found.setdefault(rid, {"sources": [], "reason": None})
        if "AI" not in entry["sources"]:
            entry["sources"].append("AI")
        entry["reason"] = entry["reason"] or reason
    return [Related(i, tuple(sorted(v["sources"])), v["reason"]) for i, v in sorted(found.items())]


@dataclass(frozen=True, slots=True)
class Pick:
    leader_id: int
    leader_change: float
    companions: list[Related] = field(default_factory=list)


def leaders(movers: Mapping[int, Mover], related: Mapping[int, Sequence[Related]]) -> list[Pick]:
    """등락률 LEADER_AT 이상이고 관계가 있는 종목, 등락률 큰 순. 동반 후보는 이끄는 종목 자신과 다른 이끄는 종목 문턱 이상인 것도 포함
    (이미 같이 오른 것도 보여 준다 — 화면이 덜 오른 순으로 놓는다)."""
    out = []
    for i, m in sorted(movers.items(), key=lambda kv: -kv[1].change_pct):
        if m.change_pct < LEADER_AT:
            continue
        rel = list(related.get(i, ()))
        if rel:
            out.append(Pick(i, m.change_pct, rel[: MAX_COMPANIONS * 3]))
    return out


def order_companions(
    rel: Sequence[Related], changes: Mapping[int, float | None]
) -> list[tuple[Related, float | None]]:
    """덜 오른 순(값이 없으면 맨 뒤), 이름·AI 둘 다인 것을 같은 등락이면 먼저. 최대 MAX_COMPANIONS."""
    ranked = sorted(
        rel,
        key=lambda r: (
            changes.get(r.instrument_id) is None,
            changes.get(r.instrument_id) or 0.0,
            -len(r.sources),
        ),
    )
    return [(r, changes.get(r.instrument_id)) for r in ranked[:MAX_COMPANIONS]]
