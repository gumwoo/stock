"""Batching a bulk insert under Postgres's parameter ceiling.

The limit is a property of the wire protocol rather than of any query: the
extended protocol carries the parameter count in sixteen bits, so one statement
binds at most 65535. A multi-row INSERT binds one per column per row, which
turns the ceiling into a row count that shrinks as the table widens.

It is a limit that hides at small scale. Two instruments never approached it;
the first collection across eighteen blew past it on the filing register, and
the failure came back from the server with the whole rendered statement
attached — tens of megabytes of SQL to say one number was too large.
"""

from __future__ import annotations

import pytest

from app.repositories import bulk
from app.repositories.candle_repo import CandleRow
from app.repositories.filing_repo import FilingRow
from app.repositories.fundamental_repo import FundamentalRow


class TestEveryBatchFits:
    @pytest.mark.parametrize("columns", [1, 7, 15, 64, 65535])
    def test_no_batch_can_exceed_the_ceiling(self, columns: int) -> None:
        rows = list(range(200_000))

        for batch in bulk.batched(rows, columns=columns):
            assert len(batch) * columns <= bulk.PARAMETER_LIMIT

    @pytest.mark.parametrize("columns", [1, 7, 15, 64])
    def test_nothing_is_dropped_or_duplicated(self, columns: int) -> None:
        rows = list(range(50_000))

        rejoined = [row for batch in bulk.batched(rows, columns=columns) for row in batch]

        assert rejoined == rows

    def test_a_row_wider_than_the_ceiling_still_yields_one_at_a_time(self) -> None:
        """Better a statement that fails on its own terms than an empty
        generator that silently writes nothing."""
        rows = [1, 2, 3]

        assert [list(b) for b in bulk.batched(rows, columns=bulk.PARAMETER_LIMIT * 2)] == [
            [1],
            [2],
            [3],
        ]

    def test_an_empty_input_yields_nothing(self) -> None:
        assert list(bulk.batched([], columns=7)) == []

    def test_a_single_batch_is_not_split(self) -> None:
        rows = list(range(10))

        assert [list(b) for b in bulk.batched(rows, columns=7)] == [rows]

    @pytest.mark.parametrize("columns", [0, -1])
    def test_a_nonsense_width_raises(self, columns: int) -> None:
        """Zero would divide by zero and negative would yield backwards; both
        are a caller error worth hearing about immediately."""
        with pytest.raises(ValueError, match="columns must be positive"):
            list(bulk.batched([1, 2, 3], columns=columns))


class TestTheWidthsTheRepositoriesPass:
    """Pinned so that adding a column fails here rather than in a collection.

    Each repository passes its row type's field count. If a column is added to
    the model and not to the row, the batch size stays at the old width and the
    statement binds more parameters than the batch was sized for — which is
    invisible until a collection happens to be large enough.
    """

    def test_a_filing_row_binds_seven(self) -> None:
        assert len(FilingRow._fields) == 7

    def test_a_fundamental_row_binds_fifteen(self) -> None:
        assert len(FundamentalRow._fields) == 15

    def test_a_candle_row_binds_ten(self) -> None:
        assert len(CandleRow.__annotations__) == 10

    def test_the_widest_row_still_allows_a_useful_batch(self) -> None:
        """A sanity floor: if some future row made batches tiny, the collection
        would still work but would issue thousands of statements."""
        widest = max(
            len(FilingRow._fields),
            len(FundamentalRow._fields),
            len(CandleRow.__annotations__),
        )

        assert bulk.PARAMETER_LIMIT // widest > 1000
