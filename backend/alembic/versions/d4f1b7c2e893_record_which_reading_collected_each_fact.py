"""Record which reading of a source's filings produced each fact

`c3e8a51d7f04` deleted the Korean net income and equity rows because they held
the including-NCI totals under parent-only names, and its note claimed the
backtest would refuse to run until they were recollected. It does not. The
coverage gate asks whether the scorer can anchor, revenue and EPS were never
touched, and the engine degrades as designed — so a run made before
recollecting returns +597.76% on Samsung where the same strategy on complete
data returns +502.43%.

No coverage check can close that. Once rows are deleted, nothing records that
the concept was ever expected for that instrument, and a gate cannot refuse an
absence it has no evidence of.

What it can refuse is a dataset that has not been recollected since the
reading changed, because the surviving rows say so about themselves. This
column carries the reading each row was written under, and the gate refuses
when any of them predates what this build does.

Existing rows default to 1, which is correct: everything already stored was
written before the reading moved. DART's current reading is 2, so every
Korean instrument is refused until recollected — which is what the earlier
note promised and this makes true.

    python -m app.cli collect --source dart --period max

SEC stays at 1. It reads us-gaap elements by name, so there is no mapping
decision to change and no boundary to cross.

Revision ID: d4f1b7c2e893
Revises: c3e8a51d7f04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4f1b7c2e893"
down_revision = "c3e8a51d7f04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default rather than a backfill statement: it applies to the rows
    # already there and to any written by a build that predates this column,
    # which is the same guarantee for both.
    op.add_column(
        "fundamental",
        sa.Column(
            "semantic_version",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
    )
    # The gate asks for the oldest row per instrument and source, so that pair
    # leads and the version is the payload.
    op.create_index(
        "ix_fundamental_semantic_version",
        "fundamental",
        ["instrument_id", "source", "semantic_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_fundamental_semantic_version", table_name="fundamental")
    op.drop_column("fundamental", "semantic_version")
