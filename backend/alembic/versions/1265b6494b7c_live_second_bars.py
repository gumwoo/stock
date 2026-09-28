"""실시간 1초봉을 저장하는 표(live_second_bar).

게이트웨이가 웹소켓 체결로 만든 1초봉을 1분마다, 그리고 장이 끝날 때 한 번 더 옮긴다. 지난날 차트를 다시 보려는
화면용 기록이고 분석 기록이 아니다(분석은 REST 1분봉 minute_bar). 연결이 끊긴 구간은 비고 되받을 곳이 없다.

Revision ID: 1265b6494b7c
Revises: 2f1293cb2c36
Create Date: 2026-09-28 15:27:38.434847
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "1265b6494b7c"
down_revision: str | None = "2f1293cb2c36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_second_bar",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("volume", sa.Numeric(precision=24, scale=4), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_live_second_bar_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_live_second_bar")),
        sa.UniqueConstraint("instrument_id", "ts", name="uq_live_second_bar_instrument_ts"),
    )
    op.create_index(
        "ix_live_second_bar_instrument_day",
        "live_second_bar",
        ["instrument_id", "session_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_live_second_bar_instrument_day", table_name="live_second_bar")
    op.drop_table("live_second_bar")
