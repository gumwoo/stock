"""증권사 투자의견(analyst_opinion)과 종목별 조회 기록(analyst_opinion_fetch). 화면 참고 표시·사후 기록 전용.

KIS 종목투자의견 원문을 리포트 한 행씩 둔다. 조회 기록은 "리포트 없음"과 "조회하지 못함"을 가른다. 목록 선정·채점에는
쓰지 않는다.

Revision ID: 8e0292ea6c39
Revises: 75495f5642fc
Create Date: 2026-09-30 11:50:45.833586
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "8e0292ea6c39"
down_revision: str | None = "75495f5642fc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analyst_opinion",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("broker", sa.String(length=40), nullable=False),
        sa.Column("opinion", sa.String(length=40), nullable=False),
        sa.Column("opinion_code", sa.String(length=4), nullable=True),
        sa.Column("prior_opinion", sa.String(length=40), nullable=True),
        sa.Column("target_price", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("prev_close", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_analyst_opinion_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analyst_opinion")),
        sa.UniqueConstraint(
            "instrument_id",
            "report_date",
            "broker",
            "opinion",
            "target_price",
            name="uq_analyst_opinion_report",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_analyst_opinion_instrument_day",
        "analyst_opinion",
        ["instrument_id", "report_date"],
        unique=False,
    )
    op.create_table(
        "analyst_opinion_fetch",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("oldest", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_analyst_opinion_fetch_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analyst_opinion_fetch")),
    )
    op.create_index(
        "ix_analyst_opinion_fetch_instrument",
        "analyst_opinion_fetch",
        ["instrument_id", "end_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_analyst_opinion_fetch_instrument", table_name="analyst_opinion_fetch")
    op.drop_table("analyst_opinion_fetch")
    op.drop_index("ix_analyst_opinion_instrument_day", table_name="analyst_opinion")
    op.drop_table("analyst_opinion")
