"""장 마감 뒤 시가총액 순위(market_cap_rank). 지수 대형주 표시와 사후 분석 전용.

KIS 시가총액 상위(보통주, KOSPI·KOSDAQ 각 30)를 세션마다 한 벌. 목록 선정·채점에는 쓰지 않는다.

Revision ID: 75495f5642fc
Revises: 5c62c3367c28
Create Date: 2026-09-28 18:11:25.481135
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "75495f5642fc"
down_revision: str | None = "5c62c3367c28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_cap_rank",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column(
            "listing",
            sa.Enum(
                "KOSPI", "KOSDAQ", "NYSE", "NASDAQ", name="listing", native_enum=False, length=8
            ),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("market_cap_eok", sa.Numeric(precision=24, scale=2), nullable=False),
        sa.Column("weight_pct", sa.Float(), nullable=False),
        sa.Column("close", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("listed_shares", sa.Numeric(precision=24, scale=0), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_market_cap_rank_instrument_id_instrument"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_cap_rank")),
        sa.UniqueConstraint(
            "session_date", "listing", "code", name="uq_market_cap_rank_day_listing_code"
        ),
    )
    op.create_index(
        "ix_market_cap_rank_instrument_day",
        "market_cap_rank",
        ["instrument_id", "session_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_market_cap_rank_instrument_day", table_name="market_cap_rank")
    op.drop_table("market_cap_rank")
