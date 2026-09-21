"""a fitted run is identified by what its folds chose

A fitted run has no single strategy, so its header carries a placeholder — the
kind, the fitter's version and a window count. Two fitters sharing a version
therefore produce identical headers however differently they behave:

    stored   win 0 MA 10/30 · win 1 MA 15/40 · win 2 MA 20/50 · …
    impostor win 0 MA 20/60 · win 1 MA 20/60 · win 2 MA 20/60 · …

    both header as moving_average_cross@ma-grid@v1 {fitted: true, windows: 5}

which let the second run's holdout be stored as the first's conclusion.

`fit_trace_fingerprint` digests what every window actually ran, in order, so
the header distinguishes them. Existing rows are backfilled from their own
window rows rather than given a placeholder: the choices are already stored,
so the value is recoverable rather than invented, and a run whose fingerprint
was faked would fail its own identity check forever after.

Autogenerate also proposed dropping `uq_backtest_window_one_holdout_per_run`,
which is a partial index it does not compare reliably. That drop is omitted —
it is the constraint that keeps a run to one holdout.

Revision ID: a36bbe9e35d4
Revises: 26316452f0f4
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a36bbe9e35d4"
down_revision: str | None = "26316452f0f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "fit_trace_fingerprint"


def _canonical(kind: str, version: str, params: dict[str, object]) -> str:
    """The same byte-stable form `StrategyDefinition.canonical` produces."""
    return json.dumps(
        {"kind": kind, "version": version, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def upgrade() -> None:
    op.add_column("backtest_run", sa.Column(COLUMN, sa.String(length=16), nullable=True))

    conn = op.get_bind()
    runs = conn.execute(sa.text("SELECT id FROM backtest_run ORDER BY id")).scalars().all()
    for run_id in runs:
        windows = conn.execute(
            sa.text(
                "SELECT window_index, sample_type, period_start, period_end, "
                "chosen_kind, chosen_version, chosen_params FROM backtest_window "
                "WHERE run_id = :r ORDER BY id"
            ),
            {"r": run_id},
        ).all()
        trace = "\n".join(
            f"{w.window_index}|{w.sample_type}|{w.period_start}|{w.period_end}|"
            + _canonical(w.chosen_kind, w.chosen_version, w.chosen_params)
            for w in windows
            if w.sample_type != "HOLDOUT"
        )
        conn.execute(
            sa.text(f"UPDATE backtest_run SET {COLUMN} = :f WHERE id = :r"),
            {"f": hashlib.sha256(trace.encode("utf-8")).hexdigest()[:16], "r": run_id},
        )

    print(f"  backfilled {COLUMN} on {len(runs)} runs from their window rows")
    op.alter_column("backtest_run", COLUMN, nullable=False)


def downgrade() -> None:
    op.drop_column("backtest_run", COLUMN)
