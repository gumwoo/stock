"""9시 전 예상체결가 조회 기록(expected_open).

평일 08:50(판정)·08:57(기록만)에 아침 목록 종목의 장전 동시호가 예상체결가를 받아 둔다. 08:50 판정에서 예상 시가 +3% 이상인
종목은 목록 행의 excluded_reason에 GAP_UP이 붙고, 판단의 원본은 이 표다.

Revision ID: b81d4f0c6a37
Revises: a7c3e91d5b20
Create Date: 2026-10-05 15:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b81d4f0c6a37"
down_revision: str | None = "a7c3e91d5b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expected_open",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purpose", sa.String(length=8), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_price", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("base_price", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("change_pct", sa.Float(), nullable=True),
        sa.Column("reported_pct", sa.Float(), nullable=True),
        sa.Column("expected_volume", sa.BigInteger(), nullable=True),
        sa.Column("mkop_code", sa.String(length=8), nullable=True),
        sa.Column("judgeable", sa.Boolean(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.instrument_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "check_at", name="uq_expected_open_check"),
    )


def downgrade() -> None:
    op.drop_table("expected_open")
