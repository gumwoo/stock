"""런이 어떤 동종군에 대해 채점됐는지 기록한다

재무 비율이 고정 척도가 아니라 동종 대비 순위로 채점되면, 점수는 더 이상 그 종목의
공시만으로 결정되지 않는다. 같은 규칙과 같은 공시라도 비교 대상이 달라지면 다른 점수가
나온다. 그래서 `universe`가 없으면 워치리스트가 바뀌는 순간 저장된 런이 재현되지 않고,
행 안의 어떤 좌표도 숫자가 왜 움직였는지 말해주지 못한다.

`experiment_fields`가 저장과 홀드아웃 검증에 같은 목록을 쓰므로, 이 컬럼은 저장되는
순간부터 비교 대상이 된다.

**NULL은 미상이 아니다.** 이 컬럼이 생기기 전에 저장된 런은 전부 고정 척도로 돌았고,
NULL이 정확히 그 상태를 말한다. 그래서 옛 런은 검사를 면제받는 것이 아니라 검사를
통과한다 — 동종군 없이 돌았다고 기록되어 있고, 재현도 동종군 없이 돌기 때문이다.

Revision ID: d9b41c7e5a20
Revises: e7a29c4b0f16
Create Date: 2026-09-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d9b41c7e5a20"
down_revision = "e7a29c4b0f16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable with no default and no backfill, deliberately. A default of
    # `[]` would claim those runs had an empty peer group, which is a third
    # thing that never happened; a backfill from today's watchlist would claim
    # they ranked against instruments that were not in the universe when they
    # ran. NULL says what is true: no ranking took place.
    op.add_column(
        "backtest_run",
        sa.Column("universe", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("backtest_run", "universe")
