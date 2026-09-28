"""아침 목록 행에 08:40 채점 상세(score_detail)를 둔다.

신호 탭을 그날 아침 목록 종목 기준으로 바꾸면서(2026-09-28), 요인 분해와 근거 문장을 풀 행과 목록 행에 남긴다.
모양은 /api/signals의 신호와 같다. 이전 행은 비어 있다.

Revision ID: 5c62c3367c28
Revises: 1265b6494b7c
Create Date: 2026-09-28 16:01:37.442436
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "5c62c3367c28"
down_revision: str | None = "1265b6494b7c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("preopen_pool_member", sa.Column("score_detail", sa.JSON(), nullable=True))
    op.add_column("watchlist_member", sa.Column("score_detail", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("watchlist_member", "score_detail")
    op.drop_column("preopen_pool_member", "score_detail")
