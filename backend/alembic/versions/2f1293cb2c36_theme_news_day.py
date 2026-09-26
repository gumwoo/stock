"""테마어 뉴스(표시 전용): 하루·테마마다 전날 장 마감 뒤 기사 수와 언급 종목

- `theme_news_day`: 07:00 체인과 08:30 보충이 같은 행을 다시 쓴다. 점수·풀·목록 순위에는 쓰지 않는다.
  기사는 `news_item`에 넣지 않고 최신 제목 몇 개와 언급 종목 상위만 JSON으로 둔다.

Revision ID: 2f1293cb2c36
Revises: 45a0d54a590e
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "2f1293cb2c36"
down_revision: str | None = "45a0d54a590e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "theme_news_day",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("theme", sa.String(length=32), nullable=False),
        sa.Column("query", sa.String(length=100), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("articles", sa.Integer(), nullable=False),
        sa.Column("capped", sa.Boolean(), nullable=False),
        sa.Column("headlines", sa.JSON(), nullable=False),
        sa.Column("mentions", sa.JSON(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_theme_news_day")),
        sa.UniqueConstraint("session_date", "theme", name="uq_theme_news_day_theme"),
    )


def downgrade() -> None:
    op.drop_table("theme_news_day")
