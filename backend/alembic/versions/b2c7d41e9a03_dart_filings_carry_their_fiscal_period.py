"""dart filings carry their fiscal period

DART's `list.json` has no period field. The period a periodic report covers is
stated only inside its published name — `사업보고서 (2025.12)` — so filings
collected before that name was parsed all landed with a null period.

That is not a cosmetic gap. `period_of_report` is what `covering_report_exists`
matches on, and a failure to match is exactly what licenses the strongest
absence claim the system makes. With every Korean filing null, a fact missing
from our value source for a period whose report was demonstrably filed was
reported as `NOT_YET_FILED` — the system asserting the company had not filed,
while holding the filing that proves it had.

The register is append-only by accession, so re-collection cannot repair the
rows already stored. The period is recovered here from the name each row
already carries; no new information is introduced.

Revision ID: b2c7d41e9a03
Revises: 5489f8841308
"""

from __future__ import annotations

import calendar
import re
from datetime import date

import sqlalchemy as sa

from alembic import op

revision = "b2c7d41e9a03"
down_revision = "5489f8841308"
branch_labels = None
depends_on = None

_REPORT_PERIOD = re.compile(r"\((\d{4})\.(\d{2})\)")


def _period_end(report_nm: str) -> date | None:
    match = _REPORT_PERIOD.search(report_nm)
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return date(year, month, calendar.monthrange(year, month)[1])


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, form FROM filing WHERE source = 'DART' AND period_of_report IS NULL")
    ).all()

    updated = 0
    for filing_id, form in rows:
        period = _period_end(form or "")
        if period is None:
            # Not every periodic disclosure names a period; those stay null
            # and simply never match, which is the honest answer for them.
            continue
        conn.execute(
            sa.text("UPDATE filing SET period_of_report = :p WHERE id = :i"),
            {"p": period, "i": filing_id},
        )
        updated += 1

    print(f"  backfilled period_of_report on {updated} of {len(rows)} DART filings")


def downgrade() -> None:
    op.execute("UPDATE filing SET period_of_report = NULL WHERE source = 'DART'")
