"""아침 목록은 하루 하나로 데이터베이스가 지키고, 장중 분석에 첫 한 시간 수익률을 더한다

- `watchlist_snapshot`에 (거래일, 전략 버전) 유일 제약. 확인과 쓰기 사이에 다른 실행이 끼어들어도 두
  번째 목록은 거부된다.
- `intraday_summary.first_hour_pct`: 시가에서 10시 전 마지막 종가까지. 30분 구간 두 개를 이어 붙이면
  구간 사이의 움직임이 빠지므로 분봉에서 직접 잰다.

Revision ID: 312255cf2435
Revises: 07c151952932
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "312255cf2435"
down_revision: str | None = "07c151952932"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("intraday_summary", sa.Column("first_hour_pct", sa.Float(), nullable=True))
    op.create_unique_constraint(
        "uq_watchlist_snapshot_day", "watchlist_snapshot", ["session_date", "strategy_version"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_watchlist_snapshot_day", "watchlist_snapshot", type_="unique")
    op.drop_column("intraday_summary", "first_hour_pct")
