"""companions

Revision ID: ddeb3649ff35
Revises: b81d4f0c6a37
Create Date: 2026-10-12 12:44:51.083943
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "ddeb3649ff35"
down_revision: str | None = "b81d4f0c6a37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companion_ask",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("batch", sa.JSON(), nullable=False),
        sa.Column("answer", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companion_ask")),
    )
    op.create_table(
        "companion_alert",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leader_id", sa.BigInteger(), nullable=False),
        sa.Column("leader_change", sa.Float(), nullable=False),
        sa.Column("companion_id", sa.BigInteger(), nullable=False),
        sa.Column("companion_change", sa.Float(), nullable=True),
        sa.Column("companion_price", sa.Float(), nullable=True),
        sa.Column("companion_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["companion_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_companion_alert_companion_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["leader_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_companion_alert_leader_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companion_alert")),
        sa.UniqueConstraint("at", "leader_id", "companion_id", name="uq_companion_alert_at"),
    )
    op.create_table(
        "companion_relation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ask_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("related_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("in_batch", sa.Boolean(), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ask_id"],
            ["companion_ask.id"],
            name=op.f("fk_companion_relation_ask_id_companion_ask"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_companion_relation_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["related_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_companion_relation_related_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companion_relation")),
        sa.UniqueConstraint("ask_id", "instrument_id", "related_id", name="uq_companion_relation"),
    )
    op.create_table(
        "open_mover",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("market_rank", sa.Integer(), nullable=False),
        sa.Column("change_pct", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("open_change_pct", sa.Float(), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name=op.f("fk_open_mover_instrument_id_instrument"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_open_mover")),
        sa.UniqueConstraint("at", "instrument_id", name="uq_open_mover_at"),
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    op.drop_table("open_mover")
    op.drop_table("companion_relation")
    op.drop_table("companion_alert")
    op.drop_table("companion_ask")
    # ### end Alembic commands ###
