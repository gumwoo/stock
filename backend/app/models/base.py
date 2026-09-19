"""Declarative base and shared column types.

Two conventions hold across every table here:

1. **All timestamps are `TIMESTAMP WITH TIME ZONE`.** A naive column in a
   point-in-time system is a latent correctness bug.

2. **`ingested_at` is not decoration.** It is the transaction-time axis. Source
   tables carry both `available_at` (when the market could have known) and
   `ingested_at` (when we actually got the row). Filtering on the first alone
   means a later backfill silently changes the result of an old backtest, since
   a backfilled filing has a past `filed_at` and passes the PIT filter despite
   not having existed when the run happened.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from sqlalchemy import BigInteger, DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Explicit naming so Alembic autogenerate produces stable, readable migrations
# instead of database-assigned constraint names that churn between revisions.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --- reusable column annotations -----------------------------------------

BigIntPk = Annotated[
    int,
    mapped_column(BigInteger, primary_key=True, autoincrement=True),
]

Timestamp = Annotated[datetime, mapped_column(DateTime(timezone=True))]

OptTimestamp = Annotated[
    datetime | None,
    mapped_column(DateTime(timezone=True), nullable=True),
]

IngestedAt = Annotated[
    datetime,
    mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        doc="Transaction time: when this row entered our database. Backtests in "
        "reproduce mode filter on this alongside available_at so that a later "
        "backfill cannot change an earlier run's result.",
    ),
]
