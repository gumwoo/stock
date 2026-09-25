"""장전 관찰 목록: 개장 전에 고른 종목과 그 이유를 그때의 숫자 그대로 고정한다

- `watchlist_snapshot`: 한 아침의 선택. 언제, 어떤 버전(오버레이·관련성 규칙·해석 모델과 프롬프트·
  관심도·국면)으로 골랐는지, 그리고 그날 아침 입력(뉴스 스윕, 모델 해석, 검색 추세, 공시)이 무엇이
  빠졌는지를 함께 남긴다. 거래일마다 하나이고 고치지 않는다.
- `watchlist_member`: 순위 1~40과 선정 이유 코드 목록, 그리고 그 순간의 오버레이 점수·사건, 검색 급증,
  발굴 점수, 국면, 전날 마감 신호의 점수. 나중에 재판정이나 모델 변경으로 현재 값이 바뀌어도
  "그날 왜 골랐는가"는 그대로 남는다.

Revision ID: e6e336131285
Revises: e4ea3cf6f85f
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e6e336131285"
down_revision: str | None = "e4ea3cf6f85f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watchlist_snapshot",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("asof", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("strategy_version", sa.String(length=32), nullable=False),
        sa.Column("selection_version", sa.Integer(), nullable=False),
        sa.Column("versions", sa.JSON(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("pool", sa.Integer(), nullable=False),
        sa.Column("left_out", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlist_snapshot")),
    )
    op.create_index(
        "ix_watchlist_snapshot_day",
        "watchlist_snapshot",
        ["session_date", "created_at"],
        unique=False,
    )
    op.create_table(
        "watchlist_member",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("tracked", sa.Boolean(), nullable=False),
        sa.Column("overlay_points", sa.Float(), nullable=True),
        sa.Column("overlay_events", sa.JSON(), nullable=False),
        sa.Column("attention_status", sa.String(length=12), nullable=True),
        sa.Column("attention_surge", sa.Float(), nullable=True),
        sa.Column("discovery_score", sa.Float(), nullable=True),
        sa.Column("regime", sa.String(length=12), nullable=True),
        sa.Column("signal_decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_score", sa.Float(), nullable=True),
        sa.Column("technical_score", sa.Float(), nullable=True),
        sa.Column("fundamental_score", sa.Float(), nullable=True),
        sa.Column("last_action", sa.String(length=20), nullable=True),
        sa.Column("news_swept_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_watchlist_member_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["watchlist_snapshot.id"],
            name=op.f("fk_watchlist_member_snapshot_id_watchlist_snapshot"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlist_member")),
        sa.UniqueConstraint("snapshot_id", "instrument_id", name="uq_watchlist_member_name"),
        sa.UniqueConstraint("snapshot_id", "rank", name="uq_watchlist_member_rank"),
    )


def downgrade() -> None:
    op.drop_table("watchlist_member")
    op.drop_index("ix_watchlist_snapshot_day", table_name="watchlist_snapshot")
    op.drop_table("watchlist_snapshot")
