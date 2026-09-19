"""Collector isolation and run recording, against a real database.

The behaviour under test is what keeps a ten-source pipeline operable:

* one source failing does not stop the other nine,
* a source with no credentials is SKIPPED rather than FAILED, and
* every outcome is written down, because an unrecorded failure is the kind
  that costs an afternoon three weeks later.

The SKIPPED/FAILED distinction is not cosmetic. With everything collapsed into
FAILED, a fresh install with no keys configured looks exactly like a system in
outage, and the dashboard loses the one useful thing it could say: which
environment variable to fill in.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.base import (
    BaseCollector,
    CollectionResult,
    CollectorError,
    SkipCollection,
    UpstreamUnavailableError,
    run_collector,
)
from app.config import get_settings
from app.models import Base, CollectorRun, CollectorStatus

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine() -> Iterator[object]:
    eng = create_engine(get_settings().database_url, future=True)
    try:
        with eng.connect() as conn:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: object) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        s.execute(CollectorRun.__table__.delete())
        s.commit()
        yield s
        s.execute(CollectorRun.__table__.delete())
        s.commit()


# --- test doubles ---------------------------------------------------------


class WorkingCollector(BaseCollector):
    name = "WORKING"

    def collect(self, session: Session) -> CollectionResult:
        return CollectionResult(items_read=182, items_saved=31)


class UnconfiguredCollector(BaseCollector):
    name = "UNCONFIGURED"

    def is_configured(self) -> bool:
        return False

    def skip_reason(self) -> str:
        return "set THREADS_ACCESS_TOKEN to enable"

    def collect(self, session: Session) -> CollectionResult:  # pragma: no cover
        raise AssertionError("collect must not run when unconfigured")


class BrokenCollector(BaseCollector):
    name = "BROKEN"

    def collect(self, session: Session) -> CollectionResult:
        raise UpstreamUnavailableError("upstream returned 503")


class ExplodingCollector(BaseCollector):
    name = "EXPLODING"

    def collect(self, session: Session) -> CollectionResult:
        raise RuntimeError("something nobody anticipated")


class PartialCollector(BaseCollector):
    name = "PARTIAL"

    def collect(self, session: Session) -> CollectionResult:
        return CollectionResult(
            items_read=100, items_saved=40, partial=True, warnings=["rate limited after page 2"]
        )


class SelfSkippingCollector(BaseCollector):
    name = "SELF_SKIPPING"

    def collect(self, session: Session) -> CollectionResult:
        raise SkipCollection("no watchlist configured yet")


# --- tests ----------------------------------------------------------------


class TestRunRecording:
    def test_success_records_counts(self, session: Session) -> None:
        run = run_collector(WorkingCollector(), session)

        assert run.status is CollectorStatus.SUCCESS
        assert run.items_read == 182
        assert run.items_saved == 31
        assert run.finished_at is not None
        assert run.error is None

    def test_partial_is_distinct_from_success(self, session: Session) -> None:
        """A run that got half the data is neither a success nor a failure."""
        run = run_collector(PartialCollector(), session)

        assert run.status is CollectorStatus.PARTIAL
        assert run.items_saved == 40
        assert run.error is not None
        assert "rate limited" in run.error

    def test_every_run_is_persisted(self, session: Session) -> None:
        for collector in (WorkingCollector(), BrokenCollector(), UnconfiguredCollector()):
            run_collector(collector, session)

        rows = session.execute(select(CollectorRun)).scalars().all()
        assert {r.source for r in rows} == {"WORKING", "BROKEN", "UNCONFIGURED"}


class TestSkippedIsNotFailed:
    def test_missing_credentials_yield_skipped(self, session: Session) -> None:
        run = run_collector(UnconfiguredCollector(), session)

        assert run.status is CollectorStatus.SKIPPED
        assert run.status is not CollectorStatus.FAILED

    def test_skip_detail_names_the_variable_to_set(self, session: Session) -> None:
        """This string is what the dashboard shows the user as the next action."""
        run = run_collector(UnconfiguredCollector(), session)

        assert run.detail is not None
        assert "THREADS_ACCESS_TOKEN" in run.detail

    def test_skipped_run_records_no_error(self, session: Session) -> None:
        """Nothing went wrong, so nothing should look like it did."""
        run = run_collector(UnconfiguredCollector(), session)
        assert run.error is None

    def test_a_collector_may_skip_itself_mid_run(self, session: Session) -> None:
        run = run_collector(SelfSkippingCollector(), session)

        assert run.status is CollectorStatus.SKIPPED
        assert run.detail == "no watchlist configured yet"


class TestFailureIsolation:
    def test_typed_failure_is_caught_and_recorded(self, session: Session) -> None:
        run = run_collector(BrokenCollector(), session)

        assert run.status is CollectorStatus.FAILED
        assert run.error is not None
        assert "UpstreamUnavailableError" in run.error
        assert "503" in run.error

    def test_unexpected_failure_is_also_contained(self, session: Session) -> None:
        """The external world produces surprises; the boundary holds anyway."""
        run = run_collector(ExplodingCollector(), session)

        assert run.status is CollectorStatus.FAILED
        assert run.error is not None
        assert "RuntimeError" in run.error

    def test_one_failure_does_not_stop_the_others(self, session: Session) -> None:
        """The property the whole pipeline depends on."""
        collectors = [
            WorkingCollector(),
            BrokenCollector(),
            ExplodingCollector(),
            UnconfiguredCollector(),
            PartialCollector(),
        ]

        runs = [run_collector(c, session) for c in collectors]

        assert len(runs) == 5
        by_source = {r.source: r.status for r in runs}
        assert by_source["WORKING"] is CollectorStatus.SUCCESS
        assert by_source["PARTIAL"] is CollectorStatus.PARTIAL
        assert by_source["BROKEN"] is CollectorStatus.FAILED
        assert by_source["EXPLODING"] is CollectorStatus.FAILED
        assert by_source["UNCONFIGURED"] is CollectorStatus.SKIPPED

    def test_run_collector_never_raises(self, session: Session) -> None:
        """Callers schedule these; an escaping exception would kill the worker."""
        try:
            run_collector(ExplodingCollector(), session)
        except CollectorError:  # pragma: no cover
            pytest.fail("collector failure escaped the isolation boundary")
