"""네이버 데이터랩 검색 추세와, 신호 옆에 남기는 검색 관심도

- `search_trend`: 한 종목의 일별 검색 추세를 받은 그대로 한 번에 한 행. 값은 요청마다
  가장 많은 날을 100으로 맞춘 상대값이라, 같은 요청 안에서만 비교된다. 그래서 점으로 쪼개지
  않고 통째로 둔다. 점이 하나도 없는 응답(검색이 너무 적음)도 답이므로 남긴다. 받은 시각은
  데이터베이스가 찍는다.
- `signal_attention`: 판단 시점까지 저장된 가장 최근 추세로 잰 급증 정도. 점수와 행동은
  바꾸지 않는다. 과거 신호에는 채우지 않는다. 오늘 받은 추세는 그때 알던 것이 아니다.

Revision ID: f69b16338a26
Revises: 8335d067491f
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f69b16338a26"
down_revision: str | None = "8335d067491f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_trend",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("series", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_search_trend_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_trend")),
    )
    op.create_index(
        "ix_search_trend_instrument_fetched",
        "search_trend",
        ["instrument_id", "fetched_at"],
        unique=False,
    )
    op.create_table(
        "signal_attention",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("signal_id", sa.BigInteger(), nullable=False),
        sa.Column("asof", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attention_version", sa.Integer(), nullable=False),
        sa.Column("trend_id", sa.BigInteger(), nullable=True),
        sa.Column("surge", sa.Float(), nullable=True),
        sa.Column("recent", sa.Float(), nullable=True),
        sa.Column("baseline", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["signal.id"],
            name=op.f("fk_signal_attention_signal_id_signal"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["trend_id"],
            ["search_trend.id"],
            name=op.f("fk_signal_attention_trend_id_search_trend"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_attention")),
        sa.UniqueConstraint("signal_id", name="uq_signal_attention_signal"),
    )


def downgrade() -> None:
    op.drop_table("signal_attention")
    op.drop_index("ix_search_trend_instrument_fetched", table_name="search_trend")
    op.drop_table("search_trend")
