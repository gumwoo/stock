"""Splitting a bulk insert so one statement fits the wire protocol.

Postgres's extended query protocol carries parameters in a 16-bit count, so a
single statement may bind at most 65535 of them. A multi-row INSERT binds one
per column per row, which makes the real limit a row count that depends on how
wide the table is: 9362 rows for the seven columns of `filing`, 4369 for the
fifteen of `fundamental`.

Nothing warns on the way there. The statement builds, the driver accepts it,
and the failure arrives from the server as
`number of parameters must be between 0 and 65535` with the entire rendered
SQL attached — thirty-six megabytes of it, in the case that prompted this.

It is also a limit that hides at small scale and appears all at once. Two
instruments never came close; the first collection across eighteen exceeded it
on the filing register before it reached the facts. So the chunking lives here
rather than in each repository, and the width is passed explicitly so that
adding a column cannot quietly halve the safe batch size without anyone
noticing.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

# The protocol ceiling itself. Not configurable — it is a property of Postgres,
# not a tuning choice.
PARAMETER_LIMIT = 65535


def batched[T](rows: Sequence[T], *, columns: int) -> Iterator[Sequence[T]]:
    """Yield slices of `rows` that each bind under the parameter ceiling.

    `columns` is how many parameters one row binds. Passing the table's column
    count is the common case; passing more is safe and passing fewer is not,
    which is why the caller states it rather than the helper guessing from a
    sample row that might be missing an optional field.
    """
    if columns <= 0:
        raise ValueError(f"columns must be positive, got {columns}")

    per_statement = max(1, PARAMETER_LIMIT // columns)
    for start in range(0, len(rows), per_statement):
        yield rows[start : start + per_statement]
