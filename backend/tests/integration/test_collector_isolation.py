"""Collector isolation and run recording, against a real database.

The behaviour under test is what keeps a ten-source pipeline operable:

* one source failing does not stop the other nine,
* a source with no credentials is SKIPPED rather than FAILED,
* a programming defect propagates instead of being filed as an outage, and
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
from sqlalchemy import create_engine, delete, select, text
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


# Only the sources these tests invent. An unscoped delete here destroyed the
# real collection history every time the suite ran, which in turn made
# fundamental freshness judge every source as never-checked — the machinery
# looked broken when the data had simply been erased underneath it.
_TEST_SOURCES = (
    "WORKING",
    "UNCONFIGURED",
    "BROKEN",
    "EXPLODING",
    "TRANSPORT",
    "PARTIAL",
    "SELF_SKIPPING",
    "KRX_MASTER",
    "POISONING",
    "SUCCEEDING_BADLY",
)


def _clear_test_runs(session: Session) -> None:
    session.execute(delete(CollectorRun).where(CollectorRun.source.in_(_TEST_SOURCES)))
    session.commit()


@pytest.fixture
def session(engine: object) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        _clear_test_runs(s)
        yield s
        _clear_test_runs(s)


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
    """A programming defect, not an outage."""

    name = "EXPLODING"

    def collect(self, session: Session) -> CollectionResult:
        raise RuntimeError("something nobody anticipated")


class TransportFailureCollector(BaseCollector):
    """A genuine transport failure the collector did not wrap."""

    name = "TRANSPORT"

    def collect(self, session: Session) -> CollectionResult:
        raise ConnectionError("connection reset by peer")


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
        expected = {"WORKING", "BROKEN", "UNCONFIGURED"}
        for collector in (WorkingCollector(), BrokenCollector(), UnconfiguredCollector()):
            run_collector(collector, session)

        # Scoped to this test's own sources. Asserting these are the *only*
        # rows would only hold while the fixture wiped the whole table, which
        # is exactly the behaviour that destroyed real collection history.
        rows = (
            session.execute(select(CollectorRun).where(CollectorRun.source.in_(expected)))
            .scalars()
            .all()
        )

        assert {r.source for r in rows} == expected


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

    def test_unwrapped_transport_failure_is_contained(self, session: Session) -> None:
        """Sockets and timeouts can only come from outside, so they are caught."""
        run = run_collector(TransportFailureCollector(), session)

        assert run.status is CollectorStatus.FAILED
        assert run.error is not None
        assert "ConnectionError" in run.error

    def test_a_programming_defect_propagates(self, session: Session) -> None:
        """Deliberately *not* contained.

        Recording a bug as FAILED would make it indistinguishable from an API
        outage, and the run log would stop meaning what it says. The run is
        still written before the exception escapes, so the crash leaves
        evidence behind.
        """
        with pytest.raises(RuntimeError, match="nobody anticipated"):
            run_collector(ExplodingCollector(), session)

        rows = (
            session.execute(select(CollectorRun).where(CollectorRun.source == "EXPLODING"))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status is CollectorStatus.FAILED

    def test_one_source_failing_does_not_stop_the_others(self, session: Session) -> None:
        """The property the whole pipeline depends on."""
        collectors = [
            WorkingCollector(),
            BrokenCollector(),
            TransportFailureCollector(),
            UnconfiguredCollector(),
            PartialCollector(),
        ]

        runs = [run_collector(c, session) for c in collectors]

        assert len(runs) == 5
        by_source = {r.source: r.status for r in runs}
        assert by_source["WORKING"] is CollectorStatus.SUCCESS
        assert by_source["PARTIAL"] is CollectorStatus.PARTIAL
        assert by_source["BROKEN"] is CollectorStatus.FAILED
        assert by_source["TRANSPORT"] is CollectorStatus.FAILED
        assert by_source["UNCONFIGURED"] is CollectorStatus.SKIPPED

    def test_external_failure_never_escapes(self, session: Session) -> None:
        """Scheduled jobs must survive an upstream being down."""
        try:
            run_collector(BrokenCollector(), session)
            run_collector(TransportFailureCollector(), session)
        except CollectorError:  # pragma: no cover
            pytest.fail("an external failure escaped the isolation boundary")


class TestARefusedArchiveIsAnOutage:
    """`corpCode.xml` answers refusals in the body, at HTTP 200.

    A wrong key, a quota refusal or a maintenance window arrives as an XML
    `<result><status>` where the ZIP was expected. Unchecked, `zipfile` raises
    `BadZipFile`, which is not a `CollectorError` — so `run_collector` re-raises
    it, the scheduled job dies, and the run is recorded as "internal error", the
    status reserved for defects in our own code.

    It is the first call every master run makes, so the whole sweep hangs on it.
    """

    @staticmethod
    def refusing(status: str) -> object:
        from app.collectors.krx_master import KrxMasterCollector

        c = KrxMasterCollector(guard=None, fill_gaps=False)  # type: ignore[arg-type]
        c._key = "test-key"
        body = (
            f"<result><status>{status}</status><message>테스트 거절</message></result>"
        ).encode()
        c._corp_code_archive = lambda _client: body  # type: ignore[assignment,method-assign]
        return c

    def test_a_quota_refusal_is_recorded_not_raised(self, session: Session) -> None:
        run = run_collector(self.refusing("020"), session)  # type: ignore[arg-type]

        assert run.status is CollectorStatus.FAILED
        assert run.error is not None
        assert "RateLimitedError" in run.error

    def test_a_bad_key_is_recorded_as_an_upstream_failure(self, session: Session) -> None:
        run = run_collector(self.refusing("010"), session)  # type: ignore[arg-type]

        assert run.status is CollectorStatus.FAILED
        assert run.error is not None
        assert "UpstreamUnavailableError" in run.error
        assert "internal error" not in run.error

    def test_the_scheduled_job_survives_it(self, session: Session) -> None:
        """The reason this matters: the worker runs unattended."""
        try:
            run_collector(self.refusing("800"), session)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            pytest.fail(f"a DART refusal escaped the isolation boundary: {exc!r}")


class PoisoningCollector(BaseCollector):
    """Fails in a way that leaves the session unable to commit anything."""

    name = "POISONING"

    def collect(self, session: Session) -> CollectionResult:
        from sqlalchemy import text

        session.execute(
            text("INSERT INTO instrument (market, name, tracked) VALUES ('KR', :n, false)"),
            {"n": "가" * 900},
        )
        return CollectionResult()  # pragma: no cover


class TestTheRunIsRecordedEvenThen:
    """An unrecorded failure is the kind that costs an afternoon later.

    A failed flush deactivates the transaction, so every later statement on it
    raises — including the commit that writes down what went wrong. Observed
    with a value too long for its column: the collector raised, the handler set
    FAILED, and the commit meant to preserve that finding raised in turn,
    leaving no row at all. The record vanished at precisely the moment it was
    worth having.
    """

    def counted(self, session: Session) -> int:
        return len(
            list(
                session.execute(
                    select(CollectorRun).where(CollectorRun.source == "POISONING")
                ).scalars()
            )
        )

    def test_a_data_error_still_leaves_a_run_row(self, session: Session) -> None:
        before = self.counted(session)

        with pytest.raises(Exception, match="too long"):
            run_collector(PoisoningCollector(), session)

        session.rollback()
        assert self.counted(session) == before + 1

    def test_the_row_says_it_failed(self, session: Session) -> None:
        with pytest.raises(Exception, match="too long"):
            run_collector(PoisoningCollector(), session)
        session.rollback()

        row = (
            session.execute(
                select(CollectorRun)
                .where(CollectorRun.source == "POISONING")
                .order_by(CollectorRun.started_at.desc())
            )
            .scalars()
            .first()
        )

        assert row is not None
        assert row.status is CollectorStatus.FAILED
        assert row.finished_at is not None

    def test_the_bad_row_was_not_written(self, session: Session) -> None:
        """The rollback that saves the record must not save the bad data."""
        from sqlalchemy import text

        with pytest.raises(Exception, match="too long"):
            run_collector(PoisoningCollector(), session)
        session.rollback()

        left = session.execute(
            text("SELECT count(*) FROM instrument WHERE name LIKE :n"), {"n": "가" * 900}
        ).scalar_one()
        assert left == 0


class SucceedingBadlyCollector(BaseCollector):
    """Reports success over rows it never committed."""

    name = "SUCCEEDING_BADLY"

    def collect(self, session: Session) -> CollectionResult:
        from app.core.calendar import Market
        from app.models import Instrument

        # Added, not executed: the row stays pending, so nothing fails until
        # something commits. That something is the run recorder, which is the
        # whole point — the collector has already returned SUCCESS by then.
        session.add(Instrument(market=Market.KR, name="가" * 900, tracked=False))
        return CollectionResult(items_read=7, items_saved=7)


class TestARunThatCouldNotCommitDidNotSucceed:
    """The rollback that saves the record must not preserve a false claim.

    `_record` rolls back so the run row survives a session the collector broke.
    That rollback also discards whatever the collector had not committed — so a
    row still saying SUCCESS would be claiming work that no longer exists.
    Measured before the fix: a collector returning SUCCESS over seven unsaved
    rows was recorded as having saved seven, and the table held none.

    Every collector commits before returning today, which makes this
    unreachable. Nothing enforces that, which is why it is handled.
    """

    def latest(self, session: Session) -> CollectorRun | None:
        return (
            session.execute(
                select(CollectorRun)
                .where(CollectorRun.source == "SUCCEEDING_BADLY")
                .order_by(CollectorRun.started_at.desc())
            )
            .scalars()
            .first()
        )

    def test_the_status_is_not_success(self, session: Session) -> None:
        run_collector(SucceedingBadlyCollector(), session)
        session.rollback()

        row = self.latest(session)
        assert row is not None
        assert row.status is CollectorStatus.FAILED

    def test_it_does_not_claim_rows_that_are_gone(self, session: Session) -> None:
        from sqlalchemy import text

        run_collector(SucceedingBadlyCollector(), session)
        session.rollback()

        row = self.latest(session)
        assert row is not None
        assert row.items_saved == 0
        assert row.error is not None

        left = session.execute(
            text("SELECT count(*) FROM instrument WHERE name LIKE :n"), {"n": "가" * 900}
        ).scalar_one()
        assert left == 0


class TestTheMasterReportsWhatItCouldNotStore:
    """A company that leaves the master without a word is a silent loss.

    Dropping a row whose fields are wider than their columns is right: one
    malformed record must not cost a sweep of four thousand. Dropping it
    quietly is the failure mode this repository argues against everywhere
    else — if DART widened a field, companies would vanish and nothing would
    say so.
    """

    @staticmethod
    def loaded(monkeypatch: pytest.MonkeyPatch, *, archive: bytes, profile: object) -> object:
        import httpx

        from app.collectors import krx_master as module

        def handler(request: httpx.Request) -> httpx.Response:
            if "list.json" in str(request.url):
                return httpx.Response(200, json={"status": "000", "total_page": 0, "list": []})
            return httpx.Response(200, json=profile)

        # The real class, captured before the patch. `module.httpx` *is* the
        # httpx module, so a lambda that calls `httpx.Client` calls the patch.
        real_client = httpx.Client
        monkeypatch.setattr(
            module.httpx,
            "Client",
            lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
        )

        class NoWait:
            def acquire(self) -> None:
                return None

        class FreeGuard:
            def reserve(
                self, group: str, endpoint: str, *, calls: int = 1, now: object = None
            ) -> None:
                return None

        c = module.KrxMasterCollector(guard=FreeGuard())  # type: ignore[arg-type]
        c._key = "test-key"
        c._bucket = NoWait()  # type: ignore[assignment]
        c._corp_code_archive = lambda _client: archive  # type: ignore[assignment,method-assign]
        return c

    def test_the_dropped_count_reaches_the_run(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.unit.test_krx_master import archive as make_archive

        collector = self.loaded(
            monkeypatch,
            archive=make_archive(("00126380", "가" * 900, "005930")),
            profile={"status": "000", "corp_cls": "Y"},
        )
        collector.name = "KRX_MASTER"  # type: ignore[attr-defined]

        with pytest.raises(Exception, match="held no listed companies"):
            collector.collect(session)  # type: ignore[attr-defined]

    def test_a_dropped_row_does_not_stop_the_good_ones(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.unit.test_krx_master import archive as make_archive

        collector = self.loaded(
            monkeypatch,
            archive=make_archive(
                ("00126380", "가" * 900, "005930"),
                ("00999801", "제트제트마스터갑", "998801"),
            ),
            profile={"status": "000", "corp_cls": "Y"},
        )
        collector.name = "KRX_MASTER"  # type: ignore[attr-defined]

        result = collector.collect(session)  # type: ignore[attr-defined]

        assert result.partial is True
        assert any("cannot store" in w for w in result.warnings)
        assert "1 dropped as malformed" in (result.detail or "")

        session.execute(
            text(
                "DELETE FROM symbol_history WHERE instrument_id IN "
                "(SELECT instrument_id FROM instrument WHERE kr_corp_code = '00999801')"
            )
        )
        session.execute(text("DELETE FROM instrument WHERE kr_corp_code = '00999801'"))
        session.commit()

    def test_a_numeric_board_leaves_the_company_unplaced(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`corp_cls` as a JSON number is not a board, and must not be read as one."""
        from tests.unit.test_krx_master import archive as make_archive

        collector = self.loaded(
            monkeypatch,
            archive=make_archive(("00999802", "제트제트마스터을", "998802")),
            profile={"status": "000", "corp_cls": 1},
        )
        collector.name = "KRX_MASTER"  # type: ignore[attr-defined]

        result = collector.collect(session)  # type: ignore[attr-defined]

        assert result.items_saved == 0
        assert "1 skipped" in (result.detail or "")
