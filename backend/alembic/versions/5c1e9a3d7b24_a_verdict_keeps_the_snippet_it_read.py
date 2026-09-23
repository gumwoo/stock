"""판정은 자기가 읽은 스니펫을 함께 남긴다

네이버는 검색어 주변을 잘라 스니펫을 만든다. 같은 기사라도 삼성전자로 검색했을 때와
SK하이닉스로 검색했을 때 `description`이 다르다. 그런데 `news_item.summary`에는 그 기사를
처음 가져온 검색의 스니펫 하나만 남는다.

재판정은 지금까지 그 `summary`로 판정했다. 두 번째 검색이 읽은 스니펫에만 회사명이
있었다면, 규칙 버전을 올리는 순간 그 판정은 "이름 없음"으로 뒤집힌다. 규칙이 바뀐 게
아니라 읽는 글이 바뀐 것이다. 검증에서 실제 행 하나(삼성전자 CONFIRMED)로 재현됐다.

그래서 판정마다 그 판정이 읽은 스니펫을 저장한다. 재판정은 이 컬럼만 읽고, 비어 있는
행은 건너뛴다. 이 컬럼 이전에 저장된 행은 무엇을 읽고 판정했는지 알 수 없으므로 다시
판정할 근거가 없다. 수집 창 안에 있는 기사는 다음 수집이 새 스니펫으로 다시 판정한다.

Revision ID: 5c1e9a3d7b24
Revises: 8063ed4ea8a7
Create Date: 2026-09-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c1e9a3d7b24"
down_revision: str | None = "8063ed4ea8a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("news_query_hit", sa.Column("snippet", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("news_query_hit", "snippet")
