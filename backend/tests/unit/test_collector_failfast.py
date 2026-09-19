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
    """Records commits without needing a database."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1


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
