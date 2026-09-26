"""테마어 뉴스 스윕: 회사 이름이 아니라 테마어로 네이버 뉴스를 찾아, 전날 장 마감 뒤 기사 수와 언급 종목을 센다.

**표시 전용**(`app/models/theme.py`). 점수·풀·목록 순위에 들어가지 않는다.

**테마어는 초안이다.** 첫 실데이터를 보고 검색어를 고친다. 한 테마 = 검색어 하나. 네이버 뉴스 검색은 검색어당 최근
1,000건까지만 주므로 과거로는 채울 수 없고, 기사가 많은 테마는 1,000건에서 잘린다(`capped`).

**이름이 `NAVER_NEWS`로 시작하지 않는다.** 수집 기록 조회(`last_success`, `last_full_success`)가 이름 앞부분으로
찾기 때문에, 그렇게 부르면 이 실행이 전체 스윕의 워터마크를 움직인다(`preopen_news`와 같은 함정).

**쿼터.** 전체 스윕과 같은 `naver_search` 그룹을 쓰고, 원장에는 `news_theme`으로 따로 남는다. 테마 14개에 최대
10쪽씩 = 한 번에 최대 140회. 07:00과 08:30 두 번이면 하루 최대 280회로 그룹 예산(12,500)의 2% 남짓이다.

**언급 종목.** 기사 제목·요약에 마스터 종목 이름이 있으면 전체 스윕과 같은 판정(`judge`)으로 CONFIRMED인 것만
센다. 다만 약한 단서만으로 확인된 이름은 뺀다(아래 `mentions`). 한 기사는 종목마다 한 번. 테마 기사에 종목 이름이 나왔다는 뜻이지 그 종목이 테마 수혜주라는 뜻은 아니다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.collectors.base import CollectionResult, SkipCollection
from app.collectors.naver_news import MAX_START, PAGE_SIZE, NaverNewsCollector
from app.collectors.quota import QuotaGuard
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.models.news import HitDecision
from app.models.theme import ThemeNewsDay
from app.repositories import instrument_repo

KR = MarketCalendar(Market.KR)
HEADLINES = 5
MENTIONS = 8
# 보통 명사와 같은 이름. 회사 이름 검색에서는 검색어가 그 회사를 가리켜 문제가 없지만, 테마 기사에서는
# "디바이스", "미래 산업"처럼 일반 단어로 나온다. 2026-09-26 첫 실데이터에서 찾은 것이고 늘어날 수 있다.
GENERIC_NAMES = frozenset({"디바이스", "미래산업"})


@dataclass(frozen=True, slots=True)
class Theme:
    key: str
    query: str


THEMES: tuple[Theme, ...] = (
    Theme("AI", "AI 관련주"),
    Theme("반도체", "반도체주"),
    Theme("HBM", "HBM"),
    Theme("엔비디아", "엔비디아"),
    Theme("원전", "원전"),
    Theme("전력기기", "전력기기"),
    Theme("2차전지", "2차전지"),
    Theme("방산", "방산"),
    Theme("조선", "조선업"),
    Theme("로봇", "로봇"),
    Theme("바이오", "바이오주"),
    Theme("관세", "관세"),
    Theme("트럼프", "트럼프"),
    Theme("금리", "금리"),
)


def morning_day(now: datetime) -> date:
    """이 조회가 속한 한국 거래일: 오늘 장이 끝나기 전이면 오늘(휴장일이면 다음 거래일), 끝났으면 다음 거래일."""
    today = KR.local_today(now)
    if KR.is_session(today) and now >= KR.session_close(today):
        return KR.next_session(today)
    return KR.session_on_or_after(today)


def window_start(day: date) -> datetime:
    """창의 시작: `day` 바로 앞 한국 거래일의 장 마감."""
    before = KR.sessions_between(day - timedelta(days=20), day - timedelta(days=1))
    return KR.session_close(before[-1])


def mentions(
    articles: Sequence[tuple[str, str]], registry: Sequence[tuple[str, str]]
) -> Counter[str]:
    """기사마다 이름이 확인된 종목을 한 번씩 센다. `registry`는 `NaverNewsCollector.registry`의 (이름, 공백 없는 소문자)."""
    counts: Counter[str] = Counter()
    # 이름마다 한 번만 센다. 기사와 이름 쌍마다 레지스트리 전체를 훑으면 시간 대부분이 여기서 나간다.
    conflicts_of: dict[str, tuple[str, ...]] = {}
    for title, summary in articles:
        squeezed = "".join(f"{title} {summary}".split()).casefold()
        for name, key in registry:
            if not key or key not in squeezed or name in GENERIC_NAMES:
                continue
            if name not in conflicts_of:
                conflicts_of[name] = NaverNewsCollector.conflicts_for((name,), registry)
            conflicts = conflicts_of[name]
            verdict = NaverNewsCollector.judge(title, summary, name=name, conflicts=conflicts)
            # 약한 단서(주가·증시 같은 시장 어휘)만으로 확인된 이름은 세지 않는다. 회사 이름 검색과 달리 테마
            # 기사는 검색 자체가 그 회사를 가리키지 않아, "대상"·"디바이스" 같은 보통 명사 이름이 시장 기사마다 잡힌다.
            if verdict.decision is HitDecision.CONFIRMED and not verdict.reason.startswith("weak:"):
                counts[name] += 1
    return counts


class ThemeNewsCollector(NaverNewsCollector):
    """테마어마다 전날 장 마감 뒤 기사를 끝까지(최대 1,000건) 읽는다."""

    name = "THEME_NEWS"
    endpoint = "news_theme"

    def __init__(
        self, *, themes: Sequence[Theme] = THEMES, guard: QuotaGuard | None = None
    ) -> None:
        super().__init__(max_pages=MAX_START // PAGE_SIZE, guard=guard)
        self.themes = tuple(themes)

    def collect(self, session: Session) -> CollectionResult:
        now = utc_now()
        day = morning_day(now)
        since = window_start(day)
        # 이름 대조는 마스터 전체가 필요하다(추적 여부와 무관하게 기사에 나온 회사를 센다).
        universe = instrument_repo.list_active(
            session, asof=now.date(), market=Market.KR, tracked=None
        )
        if not universe:
            raise SkipCollection("no active Korean instruments to match names against")
        registry = self.registry([i.name for i in universe])
        ids: dict[str, int] = {}
        for inst in universe:
            ids.setdefault(inst.name, inst.instrument_id)

        read = done = 0
        capped: list[str] = []
        stopped: str | None = None
        with httpx.Client() as client:
            for theme in self.themes:
                asked_at = utc_now()
                sweep = self._sweep(client, query=theme.query, since=since)
                read += sweep.read
                seen: dict[str, Any] = {}
                for row, _ in sweep.rows:
                    seen.setdefault(row.url_hash, row)
                rows = sorted(seen.values(), key=lambda r: r.published_at, reverse=True)
                counted = mentions([(r.title, r.summary or "") for r in rows], registry)
                values = {
                    "session_date": day,
                    "theme": theme.key,
                    "query": theme.query,
                    "since": since,
                    "asked_at": asked_at,
                    "articles": len(rows),
                    "capped": sweep.hit_page_cap,
                    "headlines": [
                        {
                            "title": r.title,
                            "url": r.url,
                            "published_at": r.published_at.isoformat(),
                            "host": r.publisher_host,
                        }
                        for r in rows
                        # 화면이 링크로 여는 값이다. http(s)가 아닌 주소(javascript: 등)는 두지 않는다.
                        if r.url.lower().startswith(("http://", "https://"))
                    ][:HEADLINES],
                    "mentions": [
                        {"instrument_id": ids[n], "name": n, "articles": c}
                        for n, c in counted.most_common(MENTIONS)
                    ],
                }
                if sweep.exhausted is not None:
                    # 쿼터가 도중에 거절했다. 반쪽 창을 전체처럼 남기지 않는다.
                    stopped = str(sweep.exhausted)
                    break
                stmt = pg_insert(ThemeNewsDay).values(**values)
                session.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_theme_news_day_theme",
                        set_={
                            **{k: stmt.excluded[k] for k in values if k != "session_date"},
                            "recorded_at": func.clock_timestamp(),
                        },
                    )
                )
                # 테마마다 커밋한다. 뒤 테마에서 네이버가 실패해도 앞서 끝난 테마를 잃지 않고, HTTP를 기다리는
                # 동안 행 잠금을 쥐고 있지 않는다. 쓰는 행은 매번 끝난 테마 하나라 반쪽 행은 생기지 않는다.
                session.commit()
                done += 1
                if sweep.hit_page_cap:
                    capped.append(theme.key)
        detail = f"{done}/{len(self.themes)} themes since {since:%Y-%m-%d %H:%M}Z for {day}"
        if capped:
            detail += f"; capped at 1,000: {', '.join(capped)}"
        if stopped:
            detail += f"; stopped: {stopped}"
        return CollectionResult(
            items_read=read, items_saved=done, partial=stopped is not None, detail=detail
        )
