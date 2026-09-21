"""DART profit and equity held the including-NCI totals under parent-only names

The collector mapped `ifrs-full_ProfitLoss` onto `NetIncomeLoss` and
`ifrs-full_Equity` onto `StockholdersEquity`, pairing them by name. In us-gaap
both of those elements are attributable to the parent; the including-NCI
figures are `ProfitLoss` and
`StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`. The
SEC collector pulls the us-gaap elements straight from companyfacts, so every
Korean row sat in the same column as an American one while meaning something
else.

Measured on FY2023 filings, the two profit figures differ by 34.8% for LG화학,
8.0% for POSCO홀딩스 and 6.5% for 삼성전자, and NAVER's parent figure is the
larger of the two. Any cross-instrument comparison across the two markets was
comparing two different quantities.

The rows cannot be relabelled, because the correct value is a different number
that lives in a tag we were not reading. They are deleted so the collector
refills them from `ProfitLossAttributableToOwnersOfParent` and
`EquityAttributableToOwnersOfParent`.

**This removes data and does not put it back.** Run

    python -m app.cli collect --source dart --period max

afterwards, and do not run a backtest before you have.

An earlier draft of this note claimed the coverage gate refuses until the data
is back. **It does not**, and the claim was checked only after it was written.
The gate asks whether the scorer can anchor; `Revenues` and
`EarningsPerShareBasic` are untouched, so it can. The engine then degrades as
designed — return on equity drops out, four ratios remain, P/E still satisfies
the profitability requirement, and a factor is produced. Measured on Samsung
over ten years, the same strategy returns +502.43% with these rows and
+597.76% without them: a 95-point difference that reads as a better strategy
and is a thinner dataset.

What does catch it is reproduction, and only for a run already stored. The
`ingested_at` axis exists so a later backfill cannot change an old result; it
can hide rows that arrived after a snapshot and can do nothing about rows that
stopped existing, because a filter cannot restore them. A stored run therefore
fails to reproduce, loudly. A new run started in this state is simply wrong.

Both halves are pinned in `tests/integration/test_semantic_correction.py`.

**Deleting was the wrong operation even though the values were wrong.** A gate
cannot refuse an absence it has no record of: once the rows are gone, nothing
says `NetIncomeLoss` was ever expected here. A semantic correction should
relabel instead — the row keeps saying what it actually holds, the scorer stops
reading it, and what an earlier run saw is still on disk. This migration is
kept as applied rather than rewritten, because rewriting a migration other
databases may have run would be a second silent divergence; the rule it teaches
is recorded in the README.

Only DART rows are touched. SEC rows were always the us-gaap elements and were
never affected.

Revision ID: c3e8a51d7f04
Revises: b7d4a1c9e520
"""

from __future__ import annotations

from alembic import op

revision = "c3e8a51d7f04"
down_revision = "b7d4a1c9e520"
branch_labels = None
depends_on = None

# The two that were filled from the including-NCI IFRS tags. Every other
# mapped concept — revenue, EPS, assets, liabilities, cash — names the same
# quantity in both taxonomies and is left alone.
AFFECTED = ("NetIncomeLoss", "StockholdersEquity")


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM fundamental
        WHERE source = 'DART'
          AND concept IN ('NetIncomeLoss', 'StockholdersEquity')
        """
    )


def downgrade() -> None:
    """Irreversible by nature.

    The deleted rows held a quantity this schema has no column for, so putting
    them back would mean re-introducing the confusion the upgrade removed. A
    downgrade leaves the two concepts empty for DART; recollecting under the
    old mapping is what would restore them, and that is a code change rather
    than a schema one.
    """
