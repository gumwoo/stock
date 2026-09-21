"""one holdout per run

The slot constraint on (run_id, window_index, sample_type) does not give this.
Two HOLDOUT rows with different window indexes both satisfy it, which is how a
second holdout — reproduced live, from a different instrument's experiment
entirely — landed on a stored run and sat beside the real one:

    holdout rows on run #36: 2
      index=5  2026-06-25..2026-09-18  return=+0.219
      index=6  2026-06-26..2026-09-18  return=-0.211

A partial unique index says the thing the schema was supposed to say: whatever
index it carries, a run has at most one holdout.

Existing duplicates are reported and the migration refuses rather than
deleting one. A holdout is a measurement somebody took; which of two is the
real one is not a question this migration can answer, and picking silently
would destroy the evidence needed to answer it.

Revision ID: 26316452f0f4
Revises: 99c1106358d9
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "26316452f0f4"
down_revision: str | None = "99c1106358d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_backtest_window_one_holdout_per_run"


def upgrade() -> None:
    conn = op.get_bind()
    duplicates = conn.execute(
        sa.text(
            "SELECT run_id, count(*) AS n FROM backtest_window "
            "WHERE sample_type = 'HOLDOUT' GROUP BY run_id HAVING count(*) > 1 "
            "ORDER BY run_id"
        )
    ).all()
    if duplicates:
        listed = ", ".join(f"run {row.run_id} has {row.n}" for row in duplicates)
        raise RuntimeError(
            f"cannot enforce one holdout per run: {listed}. Each is a measurement "
            "somebody took, and which one belongs to the run is not something this "
            "migration can decide — resolve them by hand, then re-run"
        )

    op.create_index(
        INDEX,
        "backtest_window",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("sample_type = 'HOLDOUT'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="backtest_window")
