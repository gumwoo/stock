"""anchor the holdout strategy on the run

The fit trace covers the measured windows; it deliberately excludes the
holdout, because a fitted run's final refit need not match any fold's choice.
That left the holdout's own strategy tied to nothing:

    fixed run, holdout swapped to buy_and_hold   -> reproduced=True
    fitted run, holdout swapped to buy_and_hold  -> reproduced=True

Rewrite the holdout row to a different strategy, recompute its fingerprint and
its twelve measurements, and it replays to exactly what it now claims. Every
other check passes, and the final verdict of the experiment is a measurement
of something the experiment never ran.

`holdout_strategy_fingerprint` records which strategy took that measurement,
written at the moment the holdout is stored. Nullable, because a run has no
holdout until one is taken — the reproduction reports both a holdout with no
recorded strategy and a recorded strategy with no holdout.

Existing rows are backfilled from their own holdout window, where one exists.
That is a recovery rather than a guess: the choice is already stored, and a
fabricated value would make those runs fail their own check forever.

Revision ID: b7d4a1c9e520
Revises: a36bbe9e35d4
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d4a1c9e520"
down_revision: str | None = "a36bbe9e35d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "holdout_strategy_fingerprint"


def upgrade() -> None:
    op.add_column("backtest_run", sa.Column(COLUMN, sa.String(length=16), nullable=True))

    conn = op.get_bind()
    updated = conn.execute(
        sa.text(
            f"UPDATE backtest_run SET {COLUMN} = w.chosen_fingerprint "
            "FROM backtest_window w "
            "WHERE w.run_id = backtest_run.id AND w.sample_type = 'HOLDOUT'"
        )
    ).rowcount
    print(f"  backfilled {COLUMN} on {updated} runs from their holdout rows")


def downgrade() -> None:
    op.drop_column("backtest_run", COLUMN)
