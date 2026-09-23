"""The collector boundary catches outages, not bugs.

The module docstring in `collectors/base.py` said internal invariant violations
propagate, while the code caught bare `Exception` and filed everything as
FAILED. So a typo in a collector was indistinguishable from an API outage, and
the run log meant less than it claimed.

The rule these tests pin: a `CollectorError` or a genuine transport failure is
contained; a `ValueError`, `AssertionError` or execution-timing violation is
re-raised. The run is still recorded before it propagates, so the crash leaves
evidence behind.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.collectors.base import (
    BaseCollector,
    CollectionResult,
    UpstreamUnavailableError,
    run_collector,
)
from app.models import CollectorStatus
from app.scoring.combine import ExecutionTimingError


class FakeSession:
    """Records commits without needing a database.

    `new`, `dirty` and `deleted` are here because a real `Session` always has
    them and the run recorder reads them to tell "the commit lost work" from
    "the commit lost nothing". A double that omits part of what it doubles
    fails on the day the real thing is used more fully, which is what happened.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0
        self.new: list[Any] = []
        self.dirty: list[Any] = []
        self.deleted: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.new = []
        self.dirty = []
        self.deleted = []


class ExternalOutage(BaseCollector):
    name = "EXTERNAL"

    def collect(self, session: Any) -> CollectionResult:
        raise UpstreamUnavailableError("upstream returned 503")


class SocketFailure(BaseCollector):
    name = "SOCKET"

    def collect(self, session: Any) -> CollectionResult:
        raise ConnectionError("connection reset by peer")


class TimeoutFailure(BaseCollector):
    name = "TIMEOUT"

    def collect(self, session: Any) -> CollectionResult:
        raise TimeoutError("read timed out")


class ProgrammingBug(BaseCollector):
    name = "BUG"

    def collect(self, session: Any) -> CollectionResult:
        payload: dict[str, int] = {}
        return CollectionResult(items_read=payload["missing_key"])


class BadAssumption(BaseCollector):
    name = "ASSUMPTION"

    def collect(self, session: Any) -> CollectionResult:
        raise ValueError("period must be positive")


class InvariantBreach(BaseCollector):
    name = "INVARIANT"

    def collect(self, session: Any) -> CollectionResult:
        raise ExecutionTimingError("fill precedes the decision that produced it")


class TestExternalFailuresAreContained:
    def test_typed_collector_error_is_recorded(self) -> None:
        session = FakeSession()
        run = run_collector(ExternalOutage(), session)  # type: ignore[arg-type]

        assert run.status is CollectorStatus.FAILED
        assert run.error is not None
        assert "503" in run.error

    @pytest.mark.parametrize("collector", [SocketFailure(), TimeoutFailure()])
    def test_transport_failures_are_recorded(self, collector: BaseCollector) -> None:
        """Sockets and timeouts can only come from outside the process."""
        session = FakeSession()
        run = run_collector(collector, session)  # type: ignore[arg-type]

        assert run.status is CollectorStatus.FAILED

    def test_containment_does_not_raise(self) -> None:
        run_collector(ExternalOutage(), FakeSession())  # type: ignore[arg-type]


class TestInternalFailuresPropagate:
    def test_a_missing_key_is_a_bug_not_an_outage(self) -> None:
        """A typo must not be filed as though the API were down."""
        with pytest.raises(KeyError):
            run_collector(ProgrammingBug(), FakeSession())  # type: ignore[arg-type]

    def test_a_bad_assumption_propagates(self) -> None:
        with pytest.raises(ValueError, match="period must be positive"):
            run_collector(BadAssumption(), FakeSession())  # type: ignore[arg-type]

    def test_an_invariant_breach_propagates(self) -> None:
        """Execution-timing violations must stop the work, not be logged."""
        with pytest.raises(ExecutionTimingError):
            run_collector(InvariantBreach(), FakeSession())  # type: ignore[arg-type]

    def test_the_run_is_still_recorded_before_re_raising(self) -> None:
        """A crash should leave evidence, not a silent gap in the run log."""
        session = FakeSession()

        with pytest.raises(ValueError):
            run_collector(BadAssumption(), session)  # type: ignore[arg-type]

        assert session.commits >= 1
        recorded = session.added[-1]
        assert recorded.status is CollectorStatus.FAILED
        assert recorded.finished_at is not None
        assert recorded.error == "internal error; see logs"


class FlakyCommitSession(FakeSession):
    """Commits, except the first time — and holds nothing pending.

    Stands for the ordinary shape: the collector saved its work and returned,
    and then the statement writing the run row lost its connection. Nothing the
    collector produced is at risk, because none of it was still in the session.
    """

    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    def commit(self) -> None:
        if self.failures:
            self.failures -= 1
            from sqlalchemy.exc import OperationalError

            raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))
        super().commit()


class LosingCommitSession(FlakyCommitSession):
    """The same, except the collector did leave work behind."""

    def __init__(self) -> None:
        super().__init__()
        self.new = [object()]


class Clean(BaseCollector):
    name = "CLEAN"

    def collect(self, session: Any) -> CollectionResult:
        return CollectionResult(items_read=9, items_saved=9)


class TestADowngradeDescribesWhatWasLost:
    """The rollback that rescues the run row must not rewrite what it says.

    A run is marked FAILED when its commit had to be rolled back, because the
    rollback takes whatever the collector had not saved with it. Applied to a
    session holding nothing, that is the opposite mistake: the rows are on disk
    and permanent, and the record now says they are gone. Freshness reads these
    rows, so the wrong answer travels outward from here.
    """

    def test_a_session_with_nothing_pending_keeps_its_result(self) -> None:
        session = FlakyCommitSession()

        run = run_collector(Clean(), session)  # type: ignore[arg-type]

        assert run.status is CollectorStatus.SUCCESS
        assert run.items_saved == 9

    def test_a_session_that_lost_work_is_downgraded(self) -> None:
        """The control, so the rule above cannot become "never downgrade"."""
        session = LosingCommitSession()

        run = run_collector(Clean(), session)  # type: ignore[arg-type]

        assert run.status is CollectorStatus.FAILED
        assert run.items_saved == 0

    def test_the_row_is_written_either_way(self) -> None:
        for session in (FlakyCommitSession(), LosingCommitSession()):
            run_collector(Clean(), session)  # type: ignore[arg-type]

            assert session.commits >= 1, type(session).__name__
