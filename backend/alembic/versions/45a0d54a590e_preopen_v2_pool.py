"""장전 후보 풀(PREOPEN_V2): 아침 단계의 완료 상태와 그날 들여다본 종목을 남긴다

- `preopen_pool`: 하루 한 행. 07:00 체인(전체 스윕 → 풀 확정 → 검색 추세 → 사전 수집 → LLM)과
  08:30 보충, 08:40 점수, 08:50 목록의 단계별 상태를 `stages`에 남긴다. 뒷 단계는 시각이 아니라
  이 상태를 보고 시작한다. 08:50에 풀이 없으면 `DEGRADED_FALLBACK` 풀을 만들어 계보를 잇는다.
- `preopen_pool_member`: 풀 종목마다 한 행. 들어온 경로, 07:00에 얼린 발굴 점수, 사전 수집 상태
  (`FETCHED`/`FRESH`/`SKIPPED_CAP`/`FAILED`/`NO_DATA`), 08:40 관찰용 점수와 그 시점 셋
  (기술 데이터·재무 데이터·평가), 비교군 수와 해시.
- `watchlist_member`에 V2 점수 칸의 출처와 시점, `watchlist_snapshot`에 풀 연결을 더한다.
  V1 행은 새 칸이 모두 비어 있다.

Revision ID: 45a0d54a590e
Revises: 312255cf2435
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "45a0d54a590e"
down_revision: str | None = "312255cf2435"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "preopen_pool",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asof", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("discovery", sa.JSON(), nullable=False),
        sa.Column("stages", sa.JSON(), nullable=False),
        sa.Column("pool_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preopen_pool")),
        sa.UniqueConstraint("session_date", name="uq_preopen_pool_day"),
    )
    op.create_table(
        "preopen_pool_member",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pool_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("tracked", sa.Boolean(), nullable=False),
        sa.Column("discovery_score", sa.Float(), nullable=True),
        sa.Column("has_disclosure_event", sa.Boolean(), nullable=False),
        sa.Column("disclosure_intensity", sa.Float(), nullable=True),
        sa.Column("provisional_rank", sa.Integer(), nullable=True),
        sa.Column("prefetch_status", sa.String(length=16), nullable=True),
        sa.Column("fundamental_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("price_data_asof", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fundamental_data_asof", sa.DateTime(timezone=True), nullable=True),
        sa.Column("peer_count", sa.Integer(), nullable=True),
        sa.Column("peer_hash", sa.String(length=64), nullable=True),
        sa.Column("total_score", sa.Float(), nullable=True),
        sa.Column("technical_score", sa.Float(), nullable=True),
        sa.Column("fundamental_score", sa.Float(), nullable=True),
        sa.Column("action", sa.String(length=20), nullable=True),
        sa.Column("abstained_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_preopen_pool_member_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pool_id"],
            ["preopen_pool.id"],
            name=op.f("fk_preopen_pool_member_pool_id_preopen_pool"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preopen_pool_member")),
        sa.UniqueConstraint("pool_id", "instrument_id", name="uq_preopen_pool_member_name"),
    )
    op.add_column("watchlist_member", sa.Column("score_source", sa.String(length=8), nullable=True))
    op.add_column(
        "watchlist_member", sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "watchlist_member", sa.Column("price_data_asof", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "watchlist_member",
        sa.Column("fundamental_data_asof", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "watchlist_member",
        sa.Column("fundamental_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("watchlist_member", sa.Column("peer_count", sa.Integer(), nullable=True))
    op.add_column("watchlist_member", sa.Column("peer_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "watchlist_member", sa.Column("prefetch_status", sa.String(length=16), nullable=True)
    )
    op.add_column("watchlist_member", sa.Column("abstained_reason", sa.Text(), nullable=True))
    op.add_column("watchlist_snapshot", sa.Column("pool_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_watchlist_snapshot_pool_id_preopen_pool"),
        "watchlist_snapshot",
        "preopen_pool",
        ["pool_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_watchlist_snapshot_pool_id_preopen_pool"), "watchlist_snapshot", type_="foreignkey"
    )
    op.drop_column("watchlist_snapshot", "pool_id")
    op.drop_column("watchlist_member", "abstained_reason")
    op.drop_column("watchlist_member", "prefetch_status")
    op.drop_column("watchlist_member", "peer_hash")
    op.drop_column("watchlist_member", "peer_count")
    op.drop_column("watchlist_member", "fundamental_checked_at")
    op.drop_column("watchlist_member", "fundamental_data_asof")
    op.drop_column("watchlist_member", "price_data_asof")
    op.drop_column("watchlist_member", "evaluated_at")
    op.drop_column("watchlist_member", "score_source")
    op.drop_table("preopen_pool_member")
    op.drop_table("preopen_pool")
