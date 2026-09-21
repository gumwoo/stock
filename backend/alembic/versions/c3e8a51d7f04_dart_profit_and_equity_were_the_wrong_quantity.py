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

afterwards. Until then Korean instruments have no net income or equity, which
the backtest's coverage gate refuses rather than scores around — the intended
behaviour, and the reason deleting is safe to do before recollecting.

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
