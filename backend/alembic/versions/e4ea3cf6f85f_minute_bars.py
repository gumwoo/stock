"""한국투자증권 1분봉: 종목은 장 마감 뒤 하루치를, 지수는 장중에 나눠서 받는다

- `minute_bar`: 종목의 1분봉 정본. 장중 실시간 화면의 데이터는 저장하지 않고, 장 마감 뒤 REST로
  받은 이 봉으로만 분석한다. `ts`는 그 분의 시작, `available_at`은 그 분의 끝이다(일봉과 같은 규칙).
- `index_minute_bar`: KOSPI·KOSDAQ 1분봉. 공급자가 과거 날짜를 주지 않아 장중에 나눠 받아 잇는다.
- `minute_fetch`: 종목·날짜마다 하루치가 온전히 들어왔는지(COMPLETE / PARTIAL / EMPTY). 분석은
  COMPLETE인 날만 한다. 시도할 때마다 한 행을 덧붙인다.

Revision ID: e4ea3cf6f85f
Revises: 7370d2717fea
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e4ea3cf6f85f"
down_revision: str | None = "7370d2717fea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "index_minute_bar",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("index_code", sa.String(length=16), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_index_minute_bar")),
        sa.UniqueConstraint("index_code", "ts", name="uq_index_minute_bar_code_ts"),
    )
    op.create_index(
        "ix_index_minute_bar_day", "index_minute_bar", ["index_code", "session_date"], unique=False
    )
    op.create_table(
        "minute_bar",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("volume", sa.Numeric(precision=24, scale=4), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_minute_bar_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_minute_bar")),
        sa.UniqueConstraint("instrument_id", "ts", name="uq_minute_bar_instrument_ts"),
    )
    op.create_index(
        "ix_minute_bar_instrument_day",
        "minute_bar",
        ["instrument_id", "session_date"],
        unique=False,
    )
    op.create_table(
        "minute_fetch",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("bars", sa.Integer(), nullable=False),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_minute_fetch_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_minute_fetch")),
    )
    op.create_index(
        "ix_minute_fetch_day",
        "minute_fetch",
        ["instrument_id", "session_date", "fetched_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_minute_fetch_day", table_name="minute_fetch")
    op.drop_table("minute_fetch")
    op.drop_index("ix_minute_bar_instrument_day", table_name="minute_bar")
    op.drop_table("minute_bar")
    op.drop_index("ix_index_minute_bar_day", table_name="index_minute_bar")
    op.drop_table("index_minute_bar")
