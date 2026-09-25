"""장전 후보 풀: 아침 체인이 무엇을 언제 끝냈는지와, 그날 들여다본 종목.

07:00에 시작하는 아침 체인(전체 스윕 → 풀 확정 → 검색 추세 → 사전 수집 → LLM)과
08:30 보충, 08:40 점수, 08:50 목록은 시각으로 시작하지만 서로를 시각으로 믿지
않는다. 앞 단계가 끝났는지는 이 행의 `stages`로 묻는다. 전체 스윕이 평소 10분
걸린다는 실측 하나에 기대면, 느린 날 뒷 단계가 반쯤 찬 데이터를 읽는다.

풀은 하루 하나다. 체인이 실패해 08:50에 풀이 없으면, 목록을 만드는 쪽이 이
테이블에 `DEGRADED_FALLBACK` 풀을 만들고 그 자리에서 발굴한 결과를 넣은 뒤 목록을
만든다. 정상일 때든 장애일 때든 "이 목록은 어느 풀에서 나왔나"가 같은 방식으로
남는다.

풀 종목 행은 그날 한 번 쓰이고 고치지 않는다. 사전 수집과 08:40 점수가 같은
행에 차례로 채워질 뿐이다. 점수는 `signal` 테이블에 쓰지 않는다. 16:40 신호,
포워드 기록과 섞이지 않게 하기 위해서다.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPk

NORMAL = "NORMAL"
DEGRADED_FALLBACK = "DEGRADED_FALLBACK"


class PreopenPool(Base):
    """하루의 후보 풀과 아침 단계별 상태."""

    __tablename__ = "preopen_pool"

    id: Mapped[BigIntPk]
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, doc="NORMAL, 또는 08:50에 즉석으로 만든 DEGRADED_FALLBACK."
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, doc="풀 행을 연 시각."
    )
    asof: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="풀을 얼린 순간. 아침 스윕이 아직 돌고 있으면 비어 있다.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    discovery: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, doc="풀을 얼린 발굴 결과의 조건과 개수: 상위 몇 개, 뉴스 신선도 등."
    )
    stages: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        doc="단계마다 status, started_at, finished_at, detail.",
    )
    pool_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("session_date", name="uq_preopen_pool_day"),)


class PreopenPoolMember(Base):
    """풀의 한 종목: 들어온 경로, 사전 수집 상태, 그날 관찰용 점수와 그 시점들."""

    __tablename__ = "preopen_pool_member"

    id: Mapped[BigIntPk]
    pool_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("preopen_pool.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.instrument_id", ondelete="CASCADE"), nullable=False
    )
    sources: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, doc="DISCOVERY, DISCLOSURE, FOCUS — 풀에 들어온 경로."
    )
    tracked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    discovery_score: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="풀과 함께 얼린 값. 목록의 DISCOVERY_SURGE는 이것만 본다."
    )
    has_disclosure_event: Mapped[bool] = mapped_column(Boolean, nullable=False)
    disclosure_intensity: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="사건 공시 중 가장 강한 것의 강도. 목록 순서의 동점 처리에 쓴다."
    )
    provisional_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    prefetch_status: Mapped[str | None] = mapped_column(
        String(16), nullable=True, doc="FETCHED, FRESH, SKIPPED_CAP, FAILED, NO_DATA."
    )
    fundamental_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="이 종목으로 DART에 마지막으로 닿은 시각. 재무 수치의 시점이 아니다.",
    )

    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_data_asof: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="기술 점수가 읽은 마지막 일봉(직전 거래일 종가)이 쓸 수 있게 된 시각.",
    )
    fundamental_data_asof: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="점수가 쓸 수 있던 가장 늦은 재무 수치가 쓸 수 있게 된 시각.",
    )
    peer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peer_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    technical_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    fundamental_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    abstained_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("pool_id", "instrument_id", name="uq_preopen_pool_member_name"),
    )
