"""아침 목록에서 뺀 종목의 이유(watchlist_member.excluded_reason).

선정 3(PREOPEN_V2, 2026-10-06 목록부터, 10/5는 휴장)은 40개를 고른 뒤 판단 점수 40 미만·전일 +15% 이상인 종목을 뺀다. 뺀 종목도 원래 순위
그대로 행으로 남기고 이 칸에 이유를 적는다. 화면·카톡은 이 칸이 비어 있는 행만 보여 주고, 분석은 제외 전 40개를 계속 본다.
기존 행은 모두 비어 있다(뺀 종목 없음).

Revision ID: a7c3e91d5b20
Revises: 8e0292ea6c39
Create Date: 2026-10-02 23:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a7c3e91d5b20"
down_revision: str | None = "8e0292ea6c39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "watchlist_member", sa.Column("excluded_reason", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("watchlist_member", "excluded_reason")
