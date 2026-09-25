"""Which names the morning watches, and in what order — a rule fixed before it is used.

Pure. Takes what was known at the moment of the snapshot about each name in
the pool and returns up to forty, ranked, each with every reason it is there.

**Reasons, several at once.** A name can be in for a disclosure, a search
surge and good news at the same time, and the later report needs to see the
combination, so reasons are a list of codes rather than one label:

- `DISCOVERY_SURGE` — among the news-surge candidates at that moment
- `POSITIVE_NEWS_OVERLAY` / `NEGATIVE_NEWS_OVERLAY` — overlay at or past ±1 point
- `DISCLOSURE_EVENT` — an event disclosure filed on an earlier day, stored by the moment
- `SEARCH_SURGE` — searches at least twice their earlier level
- `TRACKED_HIGH_SCORE` — a tracked name whose last signal was BUY_INTEREST
- `TRACKED` — a tracked name, the floor every tracked name meets

**Order.** More reasons first; then the size of the news overlay, then the
search surge, then the discovery score; the instrument id breaks what ties
are left, so the same inputs always give the same ranks. `TRACKED` alone
counts as no reason for ordering: it is why a name is present, not why it
stands out. Forty at most, because the live chart subscribes to one symbol
each and KIS's sample caps a connection at forty.

The thresholds repeat the ones the rest of the system already uses (the
overlay's ±1 band, the attention surge of two), so the morning does not
invent a second opinion of what counts as news.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

SELECTION_VERSION = 1
STRATEGY_VERSION = "PREOPEN_V1"
MAX_MEMBERS = 40
OVERLAY_BAND = 1.0
SEARCH_SURGE = 2.0

DISCOVERY_SURGE = "DISCOVERY_SURGE"
POSITIVE_NEWS_OVERLAY = "POSITIVE_NEWS_OVERLAY"
NEGATIVE_NEWS_OVERLAY = "NEGATIVE_NEWS_OVERLAY"
DISCLOSURE_EVENT = "DISCLOSURE_EVENT"
SEARCH_SURGE_REASON = "SEARCH_SURGE"
TRACKED_HIGH_SCORE = "TRACKED_HIGH_SCORE"
TRACKED = "TRACKED"


@dataclass(frozen=True, slots=True)
class Seen:
    """What was known about one name at the snapshot's moment."""

    instrument_id: int
    tracked: bool
    overlay_points: float | None = None
    has_disclosure_event: bool = False
    search_surge: float | None = None
    discovery_score: float | None = None
    last_action: str | None = None
    disclosure_intensity: float | None = None
    """V2 순서에서만 쓴다: 사건 공시 중 가장 강한 것의 강도(`disclosure_events`)."""


@dataclass(frozen=True, slots=True)
class Pick:
    instrument_id: int
    rank: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Selection:
    picks: list[Pick] = field(default_factory=list)
    left_out: int = 0
    """Names with a reason that did not fit in the forty."""


def reasons(seen: Seen) -> tuple[str, ...]:
    found: list[str] = []
    if seen.discovery_score is not None:
        found.append(DISCOVERY_SURGE)
    if seen.overlay_points is not None and seen.overlay_points >= OVERLAY_BAND:
        found.append(POSITIVE_NEWS_OVERLAY)
    if seen.overlay_points is not None and seen.overlay_points <= -OVERLAY_BAND:
        found.append(NEGATIVE_NEWS_OVERLAY)
    if seen.has_disclosure_event:
        found.append(DISCLOSURE_EVENT)
    if seen.search_surge is not None and seen.search_surge >= SEARCH_SURGE:
        found.append(SEARCH_SURGE_REASON)
    if seen.tracked and seen.last_action == "BUY_INTEREST":
        found.append(TRACKED_HIGH_SCORE)
    if seen.tracked:
        found.append(TRACKED)
    return tuple(found)


def _order(seen: Seen, why: tuple[str, ...]) -> tuple[float, ...]:
    standing_out = sum(1 for r in why if r != TRACKED)
    return (
        -standing_out,
        -abs(seen.overlay_points or 0.0),
        -(seen.search_surge or 0.0),
        -(seen.discovery_score or 0.0),
        seen.instrument_id,
    )


def select_names(pool: Sequence[Seen], *, limit: int = MAX_MEMBERS) -> Selection:
    """Every name with a reason, ranked, the first `limit` kept."""
    chosen = [(s, reasons(s)) for s in pool]
    chosen = [(s, why) for s, why in chosen if why]
    chosen.sort(key=lambda pair: _order(*pair))
    picks = [
        Pick(instrument_id=s.instrument_id, rank=n, reasons=why)
        for n, (s, why) in enumerate(chosen[:limit], 1)
    ]
    return Selection(picks=picks, left_out=max(0, len(chosen) - limit))


# --- PREOPEN_V2 ---------------------------------------------------------------
#
# V2는 "오늘 무슨 일이 생긴 종목"만 본다. 추적 중이라는 사실이나 전날 점수는
# 목록에 들어갈 이유가 아니다. 단타 관찰에서 조용한 종목이 매일 자리를 차지하면
# 목록의 뜻이 흐려지기 때문이다. 추적 여부는 종목 행에 속성으로만 남는다.
#
# 이유 코드와 기준값은 V1과 같다. 바뀐 것은 무엇을 이유로 치느냐뿐이라, 두
# 버전의 기록을 나란히 놓고 비교할 수 있다. 조용한 날은 0개가 정상이다.

SELECTION_VERSION_V2 = 2
STRATEGY_VERSION_V2 = "PREOPEN_V2"
EVENT_REASONS = (
    DISCOVERY_SURGE,
    POSITIVE_NEWS_OVERLAY,
    NEGATIVE_NEWS_OVERLAY,
    DISCLOSURE_EVENT,
    SEARCH_SURGE_REASON,
)


def reasons_v2(seen: Seen) -> tuple[str, ...]:
    """V1의 이유 중 그날 사건에 해당하는 것만. 추적과 점수는 이유가 아니다."""
    return tuple(r for r in reasons(seen) if r in EVENT_REASONS)


def _order_v2(seen: Seen, why: tuple[str, ...]) -> tuple[float, ...]:
    # 마지막 동점 처리 앞에 공시 강도를 둔다. 공시만 있는 종목은 뉴스·검색·발굴
    # 숫자가 모두 비어 있어, 목록이 40개로 차면 누가 남을지를 종목 id가 정했다.
    # 공휴일 뒤 아침처럼 공시가 몰리는 날 그 순서는 사실상 임의다.
    return (
        -len(why),
        -abs(seen.overlay_points or 0.0),
        -(seen.search_surge or 0.0),
        -(seen.discovery_score or 0.0),
        -(seen.disclosure_intensity or 0.0),
        seen.instrument_id,
    )


def select_names_v2(pool: Sequence[Seen], *, limit: int = MAX_MEMBERS) -> Selection:
    """그날 이유가 하나라도 있는 종목만, 순위대로 `limit`개까지. 0개도 결과다."""
    chosen = [(s, reasons_v2(s)) for s in pool]
    chosen = [(s, why) for s, why in chosen if why]
    chosen.sort(key=lambda pair: _order_v2(*pair))
    picks = [
        Pick(instrument_id=s.instrument_id, rank=n, reasons=why)
        for n, (s, why) in enumerate(chosen[:limit], 1)
    ]
    return Selection(picks=picks, left_out=max(0, len(chosen) - limit))
