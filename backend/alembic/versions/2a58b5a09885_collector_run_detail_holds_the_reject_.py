"""수집 실행 기록이 종목별 거부율을 담을 수 있게 한다

Phase 4 계획은 뉴스 수집을 한 번에 켜지 않는다. 1종목, 10종목, 전체 순으로 올리고,
단계마다 **종목별 거부율 상위 20개**를 본 뒤 다음으로 넘어간다. 거부율이 유난히 높은
종목은 질의가 나쁘거나 alias가 빠진 것이고, 언급은 많은데 거부가 0인 종목은 오탐이
그대로 통과했을 수 있다.

그 숫자는 지금 어디에도 남지 않는다. 거부된 기사는 mention을 만들지 않고, 어떤 질의가
그 기사를 가져왔는지는 mention에만 기록되므로 나중에 DB에서 복원할 수도 없다. 실행 순간에
적어두는 수밖에 없다.

적을 곳은 `collector_run.detail`인데 500자 제한이었다. 상위 20개 두 목록은 그 안에
들어가지 않고, 들어가는 앞부분만 적으면 롤아웃이 찾으려는 바로 그 종목들이 잘려 나간다.
그래서 Text로 넓힌다. Postgres에서 VARCHAR → TEXT는 테이블을 다시 쓰지 않는다.

**되돌리기는 긴 기록을 잘라낸다.** 500자를 넘는 행이 하나라도 있으면 타입을 좁히는
ALTER가 실패하므로, 먼저 500자로 자른다. 잘린 뒷부분은 복구되지 않는다 — 되돌리기를
하는 사람이 알아야 할 대가다.

Revision ID: 2a58b5a09885
Revises: 3d25cb6778a9
Create Date: 2026-09-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2a58b5a09885"
down_revision: str | None = "3d25cb6778a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "collector_run",
        "detail",
        existing_type=sa.VARCHAR(length=500),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.execute("UPDATE collector_run SET detail = left(detail, 500) WHERE length(detail) > 500")
    op.alter_column(
        "collector_run",
        "detail",
        existing_type=sa.Text(),
        type_=sa.VARCHAR(length=500),
        existing_nullable=True,
    )
