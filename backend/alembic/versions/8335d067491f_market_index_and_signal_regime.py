"""시장 지수 일봉과, 신호 옆에 남기는 시장 국면

- `market_index_bar`: KOSPI, KOSDAQ, S&P 500의 일봉. 지수는 상장 종목이 아니므로
  `instrument`에 넣지 않는다. 넣으면 뉴스 스윕이 이름으로 검색하고 마스터 수에 섞인다.
  지수 값은 재작성되지 않으므로 세션당 한 행만 두고 다시 받은 것은 무시한다.
- `signal_regime`: 신호의 판단 시점에 그 종목이 속한 지수의 추세, 변동성, 폭과 국면 이름.
  오버레이처럼 점수 옆에 두고 행동은 바꾸지 않는다. 판단 시점까지 끝난 종가만 쓰므로
  이 테이블이 생기기 전의 신호에도 채울 수 있다.

Revision ID: 8335d067491f
Revises: 95ba16fb4d49
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "8335d067491f"
down_revision: str | None = "95ba16fb4d49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_index_bar",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("index_code", sa.String(length=16), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_index_bar")),
        sa.UniqueConstraint("index_code", "ts", name="uq_market_index_bar_code_ts"),
    )
    op.create_index(
        "ix_market_index_bar_available",
        "market_index_bar",
        ["index_code", "available_at"],
        unique=False,
    )
    op.create_table(
        "signal_regime",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("signal_id", sa.BigInteger(), nullable=False),
        sa.Column("asof", sa.DateTime(timezone=True), nullable=False),
        sa.Column("regime_version", sa.Integer(), nullable=False),
        sa.Column("index_code", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=12), nullable=False),
        sa.Column("index_close", sa.Float(), nullable=True),
        sa.Column("trend_gap", sa.Float(), nullable=True),
        sa.Column("return_20d", sa.Float(), nullable=True),
        sa.Column("volatility_20d", sa.Float(), nullable=True),
        sa.Column("volatility_rank", sa.Float(), nullable=True),
        sa.Column("breadth", sa.Float(), nullable=True),
        sa.Column("breadth_names", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["signal.id"],
            name=op.f("fk_signal_regime_signal_id_signal"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_regime")),
        sa.UniqueConstraint("signal_id", name=op.f("uq_signal_regime_signal_id")),
    )


def downgrade() -> None:
    op.drop_table("signal_regime")
    op.drop_index("ix_market_index_bar_available", table_name="market_index_bar")
    op.drop_table("market_index_bar")
