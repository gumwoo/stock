"""장중 분석: 종목의 하루를 1분봉으로 요약한다

`intraday_summary`는 `minute_bar`에서 언제든 다시 계산할 수 있는 파생값이다. 재는 방식이 바뀌면 고치지
않고 `analysis_version`을 올린다. 하루치가 온전히 오지 않은 날은 상태만 남기고 수치는 계산하지
않으며, 그날이 나중에 다 오면 전체 분석으로 바뀐다. MFE와 MAE는 사후 값이다. 그날의 최고가와 최저가를
시가에 견준 것이라, 실제로 얻을 수 있었던 수익이 아니다.

Revision ID: 07c151952932
Revises: e6e336131285
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "07c151952932"
down_revision: str | None = "e6e336131285"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intraday_summary",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("analysis_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("bars", sa.Integer(), nullable=False),
        sa.Column("return_pct", sa.Float(), nullable=True),
        sa.Column("mfe_pct", sa.Float(), nullable=True),
        sa.Column("mae_pct", sa.Float(), nullable=True),
        sa.Column("high_at", sa.String(length=5), nullable=True),
        sa.Column("low_at", sa.String(length=5), nullable=True),
        sa.Column("minutes_to_high", sa.Integer(), nullable=True),
        sa.Column("close_vs_vwap_pct", sa.Float(), nullable=True),
        sa.Column("volatility_pct", sa.Float(), nullable=True),
        sa.Column("peak_volume_at", sa.String(length=5), nullable=True),
        sa.Column("index_code", sa.String(length=16), nullable=True),
        sa.Column("market_return_pct", sa.Float(), nullable=True),
        sa.Column("market_source", sa.String(length=12), nullable=True),
        sa.Column("buckets", sa.JSON(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_intraday_summary_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intraday_summary")),
        sa.UniqueConstraint(
            "instrument_id", "session_date", "analysis_version", name="uq_intraday_summary_day"
        ),
    )


def downgrade() -> None:
    op.drop_table("intraday_summary")
