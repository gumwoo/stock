"""한국투자증권이 발급한 토큰과 웹소켓 접속키를 보관한다

토큰은 1분에 한 번만 발급되고 하루 동안 재사용하라고 되어 있다. 프로세스가 시작할 때마다 새로
받으면 곧 거절당하므로, 발급받은 값을 만료 시각과 함께 두고 만료 전까지 다시 쓴다. 앱키와
시크릿은 환경 변수에만 있고 여기에는 복사하지 않는다. 이 테이블의 값은 로그와 오류 메시지에
나오지 않는다.

Revision ID: 7370d2717fea
Revises: f69b16338a26
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "7370d2717fea"
down_revision: str | None = "f69b16338a26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kis_credential",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("env", sa.String(length=8), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kis_credential")),
    )
    op.create_index(
        "ix_kis_credential_lookup", "kis_credential", ["env", "kind", "issued_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_kis_credential_lookup", table_name="kis_credential")
    op.drop_table("kis_credential")
