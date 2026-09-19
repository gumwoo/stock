"""fundamental uniqueness must treat NULL period_start as equal

Revision ID: a675f09d5f60
Revises: f6ab8ef222d1
Create Date: 2026-09-19 16:47:43.272133
"""

from collections.abc import Sequence

from alembic import op


revision: str = "a675f09d5f60"
down_revision: str | None = "f6ab8ef222d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make the uniqueness constraint see NULL period_start values as equal.

    `period_start` is NULL for instantaneous facts — a balance sheet figure is
    measured at a date, not across a span. Postgres's default UNIQUE semantics
    treat every NULL as distinct from every other, so those rows never
    conflicted and duplicated on every collection run. Verified in the data:
    1,492 rows covering 746 distinct contexts, exactly doubled by a second run.

    NULLS NOT DISTINCT (Postgres 15+) fixes it. Existing duplicates are removed
    first, keeping the lowest id of each group — they are byte-identical
    re-collections, not restatements, so nothing of value is lost.
    """
    op.execute(
        """
        DELETE FROM fundamental f
        USING fundamental keep
        WHERE keep.id < f.id
          AND keep.instrument_id = f.instrument_id
          AND keep.taxonomy      = f.taxonomy
          AND keep.concept       = f.concept
          AND keep.unit          = f.unit
          AND keep.period_end    = f.period_end
          AND keep.form          = f.form
          AND keep.filed_at      = f.filed_at
          AND keep.period_start IS NOT DISTINCT FROM f.period_start
          AND keep.accession    IS NOT DISTINCT FROM f.accession
        """
    )

    op.drop_constraint("uq_fundamental_context_filing", "fundamental", type_="unique")
    op.execute(
        """
        ALTER TABLE fundamental
        ADD CONSTRAINT uq_fundamental_context_filing
        UNIQUE NULLS NOT DISTINCT
        (instrument_id, taxonomy, concept, unit, period_start, period_end,
         form, filed_at, accession)
        """
    )


def downgrade() -> None:
    op.drop_constraint("uq_fundamental_context_filing", "fundamental", type_="unique")
    op.create_unique_constraint(
        "uq_fundamental_context_filing",
        "fundamental",
        [
            "instrument_id",
            "taxonomy",
            "concept",
            "unit",
            "period_start",
            "period_end",
            "form",
            "filed_at",
            "accession",
        ],
    )
