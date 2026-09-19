"""fundamental uniqueness must treat NULL period_start as equal

Revision ID: a675f09d5f60
Revises: f6ab8ef222d1
Create Date: 2026-09-19 16:47:43.272133
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


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
    first, keeping the lowest id of each group.

    The delete is guarded rather than assumed safe. Its matching condition is
    the context and the filing, which does not by itself prove the rows carry
    the same value — a group holding two different values would mean something
    stranger than a re-collection, and silently dropping one would destroy
    evidence. So the migration asserts no such group exists before deleting,
    and fails loudly if one does.
    """
    conflicting = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT count(*) FROM (
              SELECT 1 FROM fundamental
              GROUP BY instrument_id, taxonomy, concept, unit, period_start,
                       period_end, form, filed_at, accession
              HAVING count(DISTINCT value) > 1
            ) q
            """
            )
        )
        .scalar_one()
    )
    if conflicting:
        raise RuntimeError(
            f"{conflicting} context/filing groups hold more than one distinct "
            "value. These are not plain re-collections, and deleting by context "
            "alone would discard a real difference. Investigate before migrating."
        )

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
