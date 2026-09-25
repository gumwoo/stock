"""08:30 장전 보충 스윕: 오늘 후보 풀 종목만, 07:00 스윕이 시작한 뒤의 기사만.

전체 스윕(`NAVER_NEWS`)과 같은 검색·판정 규칙을 쓰고, 두 가지만 다르다.

**이름이 다르다. 그리고 `NAVER_NEWS`로 시작하지 않는다.** 수집 기록을 읽는
`last_success`와 `last_full_success`는 이름을 `LIKE 'NAVER_NEWS%'`로 찾는다.
이 실행을 `NAVER_NEWS_…`로 부르면 풀 60여 종목만 본 보충 실행의 SUCCESS가
전체 스윕의 워터마크를 끌어올린다. 읽은 구간 기록(`news_sweep_coverage`)도 이
이름으로 남으므로, 발굴의 "평소 대비 급증" 기준선은 이 실행이 읽은 구간을
세지 않는다.

**어디서부터 읽을지를 묻지 않고 받는다.** 새 이름의 첫 실행은 워터마크가 없어
3일을 거슬러 읽는다. 보충은 그럴 이유가 없다. 오늘 07:00 스윕이 시작한 시각을
받아 그 뒤만 읽는다.

풀 밖 종목에서 07:00 뒤에 나온 뉴스는 보지 않는다. 알고 두는 사각지대다.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from sqlalchemy.orm import Session

from app.collectors.base import CollectionResult, SkipCollection
from app.collectors.naver_news import NaverNewsCollector
from app.collectors.quota import QuotaGuard
from app.core.clock import ensure_utc
from app.models import Instrument


class PreopenNewsSupplement(NaverNewsCollector):
    """후보 풀 종목의 아침 기사만 다시 훑는다."""

    name = "PREOPEN_NEWS_SUPPLEMENT"

    def __init__(
        self,
        *,
        instrument_ids: Collection[int],
        since: datetime,
        max_pages: int | None = None,
        guard: QuotaGuard | None = None,
    ) -> None:
        super().__init__(max_pages=max_pages, guard=guard)
        self.instrument_ids = frozenset(instrument_ids)
        self.since = ensure_utc(since, field="since")

    def targets(self, universe: list[Instrument]) -> list[Instrument]:
        return [i for i in universe if i.instrument_id in self.instrument_ids]

    def expected(self, universe: list[Instrument], targets: list[Instrument]) -> list[Instrument]:
        # 풀 종목을 다 물었으면 끝난 것이다. 마스터 전체를 기준으로 세면 매번
        # PARTIAL이 되어 상태가 아무것도 말하지 않는다.
        return targets

    def watermark(self, session: Session, *, now: datetime) -> datetime:
        return self.since

    def collect(self, session: Session) -> CollectionResult:
        if not self.instrument_ids:
            raise SkipCollection("no names in today's pool")
        return super().collect(session)
