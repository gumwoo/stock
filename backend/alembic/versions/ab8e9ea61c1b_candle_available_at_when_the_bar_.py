"""candle.available_at: when the bar completed, not when it opened

Revision ID: ab8e9ea61c1b
Revises: f5f0cc07229c
Create Date: 2026-09-19 14:52:11.095526
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "ab8e9ea61c1b"
down_revision: str | None = "f5f0cc07229c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `available_at` and backfill it from the market calendar.

    A daily bar's close, high, low and volume do not exist until the session
    ends, so `ts` (the session open) is the wrong thing for a simulation to
    filter on: at 10:00 the bar has opened but its close has not happened.

    Existing rows are backfilled by deriving each market's session close from
    the stored open, which is a fixed offset per market — KRX 09:00-15:30 KST
    and NYSE 09:30-16:00 ET are both 6h30m sessions. The column is added
    nullable, filled, then made NOT NULL.
    """
    op.add_column(
        "candle",
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
    )

    # NOTE: SQLAlchemy's Enum stores the member NAME, so this column holds
    # 'DAY_1' and 'MIN_1', not the '1d' / '1m' values the Python enum carries.
    # Raw SQL in migrations has to use the names.
    #
    # Both KRX and NYSE run 6h30m regular sessions, and `ts` already holds the
    # true session open in UTC, so the offset is uniform. DST is baked into
    # `ts` because it was written from the exchange calendar.
    op.execute(
        """
        UPDATE candle
        SET available_at = ts + interval '6 hours 30 minutes'
        WHERE interval = 'DAY_1' AND available_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE candle
        SET available_at = ts + interval '1 minute'
        WHERE interval = 'MIN_1' AND available_at IS NULL
        """
    )
    op.execute("UPDATE candle SET available_at = ts WHERE available_at IS NULL")

    op.alter_column("candle", "available_at", nullable=False)
    op.create_index(
        "ix_candle_available",
        "candle",
        ["instrument_id", "interval", "available_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_candle_available", table_name="candle")
    op.drop_column("candle", "available_at")
