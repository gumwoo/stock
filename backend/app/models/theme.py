"""테마어 뉴스: 회사 이름이 아니라 테마어("AI 반도체", "원전" 등)로 찾은 아침 기사 요약.

**표시 전용이다.** 발굴 점수, 풀, 08:40 점수, 목록 순위 어디에도 들어가지 않는다. 밤사이 업종 연구와 NXT
후속 연구는 이런 신호로 9시나 8시에 사서 비용 뒤·평소(비교군) 대비 남는다는 근거를 찾지 못했다. 검증 없이
점수에 넣지 않고, 화면에 보여 주고 판단은 사람이 한다. 쌓인 행은 나중에 앞으로의 기록으로
따로 검증할 수 있다.

하루·테마마다 한 행. 07:00 체인과 08:30 보충이 같은 행을 다시 쓴다(같은 창을 더 늦게 본 값). 창은 전날
한국 장 마감부터 조회 시각까지다. 기사는 `news_item`에 넣지 않는다. 넣으면 종목 뉴스 쪽 중복 제거와 섞인다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk


class ThemeNewsDay(Base):
    __tablename__ = "theme_news_day"
    __table_args__ = (UniqueConstraint("session_date", "theme", name="uq_theme_news_day_theme"),)

    id: Mapped[BigIntPk]
    session_date: Mapped[date] = mapped_column(
        Date, nullable=False, doc="이 아침이 속한 한국 거래일"
    )
    theme: Mapped[str] = mapped_column(String(32), nullable=False)
    query: Mapped[str] = mapped_column(String(100), nullable=False, doc="네이버 뉴스에 보낸 검색어")
    since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, doc="창의 시작: 전날 한국 장 마감"
    )
    asked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, doc="창의 끝: 조회 시각"
    )
    articles: Mapped[int] = mapped_column(Integer, nullable=False, doc="창 안의 기사 수")
    capped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, doc="API 상한(1,000건)에 걸려 실제 기사는 더 많을 수 있다"
    )
    headlines: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, doc="최신 기사 몇 개: title, url, published_at, host"
    )
    mentions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        doc="창 안 기사에서 이름이 확인된 종목 상위: instrument_id, name, articles",
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
