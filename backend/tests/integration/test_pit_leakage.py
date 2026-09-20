"""The door holds, including against data planted specifically to get through.

The plan asks for two things here, and they fail differently.

A **look-ahead** leak is caught by `available_at`: data the market could not
have known. Easy to reason about, easy to test.

A **backfill** leak is caught by `ingested_at`, and it is the one that survives
review. A filing collected later carries a *past* `filed_at`, so it satisfies
every look-ahead check ever written. Re-running a stored backtest then produces
different numbers from what it reported, with no error and nothing missing —
the run simply becomes a different run wearing the same id.

Both are planted here deliberately, and the second is also asserted from the
other side: the unbounded read *must* change, or the test would pass against a
database where the backfill silently failed to land.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtest.pit_repository import PitReader, PitViolationError, snapshot_now
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.models import Base, Instrument, Interval
from app.models.fundamental import FiscalPeriod, FundamentalSource
from app.repositories import candle_repo
from app.repositories import fundamental_repo as frepo
from app.repositories.candle_repo import CandleRow
from app.repositories.fundamental_repo import FundamentalContext, FundamentalRow

pytestmark = pytest.mark.integration

US = MarketCalendar(Market.US)

# The simulation stands here: the 12th has closed, the 13th has not.
#
# `data_snapshot_at` is *not* a date in this story. The simulation instant is
# historical because that is when the market was; the snapshot is a
# transaction time, and these rows were written just now. Conflating the two
# is easy and yields an empty read rather than a wrong one, which is at least
# a loud failure — the fixture takes the snapshot from the database clock, so
# it is always genuinely after the seed.
SIM_ASOF = US.session_close(date(2025, 11, 12))

FY2024 = FundamentalContext(
    taxonomy="us-gaap",
    concept="Revenues",
    unit="USD",
    period_end=date(2024, 12, 31),
    period_start=date(2024, 1, 1),
)


def _bar(iid: int, day: date, close: str) -> CandleRow:
    return CandleRow(
        instrument_id=iid,
        interval=Interval.DAY_1,
        ts=US.session_open(day),
        available_at=US.session_close(day),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("1000"),
        source="TEST",
    )


@pytest.fixture(scope="module")
def engine() -> Iterator[object]:
    eng = create_engine(get_settings().database_url, future=True)
    try:
        with eng.connect():
            pass
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def planted(engine: object) -> Iterator[tuple[Session, int, datetime]]:
    """Honest history, plus one bar from the future."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.US, name="LEAK TEST CORP", us_cik="9999999996")
        s.add(inst)
        s.flush()
        iid = inst.instrument_id

        candle_repo.save_revisions(
            s,
            [
                _bar(iid, date(2025, 11, 10), "100"),
                _bar(iid, date(2025, 11, 11), "101"),
                _bar(iid, date(2025, 11, 12), "102"),
                # Tomorrow. A strategy that can see this can see the future.
                _bar(iid, date(2025, 11, 13), "140"),
            ],
        )
        frepo.save_facts(
            s,
            [
                FundamentalRow(
                    instrument_id=iid,
                    taxonomy="us-gaap",
                    concept="Revenues",
                    unit="USD",
                    period_start=date(2024, 1, 1),
                    period_end=date(2024, 12, 31),
                    fiscal_year=2024,
                    fiscal_period=FiscalPeriod.FY,
                    form="10-K",
                    value=Decimal("500000000"),
                    filed_at=date(2025, 2, 14),
                    available_at=US.next_session_open(date(2025, 2, 14)),
                    accession="0000000000-25-000001",
                    source=FundamentalSource.SEC,
                )
            ],
        )
        s.commit()

        # Taken after the seed and before any test's backfill. The commit
        # matters: Postgres `now()` is the *transaction* start time, so a
        # backfill written in the same open transaction would be stamped with
        # the snapshot's own instant and slip past `ingested_at <= snapshot`.
        snapshot = snapshot_now(s)
        s.commit()

        yield s, iid, snapshot

        for table in ("candle", "fundamental", "filing", "instrument"):
            s.execute(text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": iid})
        s.commit()


def reader(s: Session, snapshot: datetime) -> PitReader:
    return PitReader(s, data_snapshot_at=snapshot).at(SIM_ASOF)


class TestLookAhead:
    def test_tomorrows_bar_is_not_returned(self, planted: tuple[Session, int, datetime]) -> None:
        s, iid, snapshot = planted
        bars = reader(s, snapshot).bars(iid, Interval.DAY_1)

        assert [b.close for b in bars] == [Decimal("100"), Decimal("101"), Decimal("102")]

    def test_the_planted_bar_really_is_in_the_table(
        self, planted: tuple[Session, int, datetime]
    ) -> None:
        """Otherwise the test above passes against an empty future."""
        s, iid, _ = planted
        assert candle_repo.count_for(s, iid, Interval.DAY_1) == 4

    def test_every_bar_returned_was_complete_by_the_simulation_instant(
        self, planted: tuple[Session, int, datetime]
    ) -> None:
        s, iid, snapshot = planted
        assert all(
            b.available_at <= SIM_ASOF for b in reader(s, snapshot).bars(iid, Interval.DAY_1)
        )

    def test_an_open_but_unfinished_bar_is_withheld(
        self, planted: tuple[Session, int, datetime]
    ) -> None:
        """Mid-session the day's bar exists and its close does not."""
        s, iid, snapshot = planted
        midday = US.session_open(date(2025, 11, 12)) + timedelta(hours=1)
        bars = PitReader(s, data_snapshot_at=snapshot).at(midday).bars(iid, Interval.DAY_1)

        assert [b.close for b in bars] == [Decimal("100"), Decimal("101")]

    def test_simulating_past_the_snapshot_raises(
        self, planted: tuple[Session, int, datetime]
    ) -> None:
        s, _, snapshot = planted
        with pytest.raises(PitViolationError, match="after the data snapshot"):
            PitReader(s, data_snapshot_at=snapshot).at(snapshot + timedelta(days=1))

    def test_a_reader_with_no_instant_answers_nothing(
        self, planted: tuple[Session, int, datetime]
    ) -> None:
        s, iid, snapshot = planted
        with pytest.raises(PitViolationError, match="no simulation instant"):
            PitReader(s, data_snapshot_at=snapshot).bars(iid, Interval.DAY_1)


class TestBackfill:
    """The leak that passes every look-ahead check ever written."""

    def test_a_backfilled_revision_does_not_reach_an_earlier_snapshot(
        self, planted: tuple[Session, int, datetime]
    ) -> None:
        s, iid, snapshot = planted
        before = reader(s, snapshot).bars(iid, Interval.DAY_1)[-1].close

        # A correction to the 12th, arriving now, with the same bar timestamp.
        candle_repo.save_revisions(s, [_bar(iid, date(2025, 11, 12), "999")])
        s.commit()

        after = reader(s, snapshot).bars(iid, Interval.DAY_1)[-1].close
        assert after == before == Decimal("102")

    def test_the_backfill_did_change_the_unbounded_view(
        self, planted: tuple[Session, int, datetime]
    ) -> None:
        """Proves the correction landed, so the test above is not vacuous."""
        s, iid, _ = planted
        candle_repo.save_revisions(s, [_bar(iid, date(2025, 11, 12), "999")])
        s.commit()

        corrected = {b.ts: b.close for b in candle_repo.history(s, iid, Interval.DAY_1, limit=10)}
        assert corrected[US.session_open(date(2025, 11, 12))] == Decimal("999")

    def test_a_later_snapshot_does_see_it(self, planted: tuple[Session, int, datetime]) -> None:
        """The filter bounds the view; it does not hide the data forever."""
        s, iid, _ = planted
        candle_repo.save_revisions(s, [_bar(iid, date(2025, 11, 12), "999")])
        s.commit()

        wide = PitReader(s, data_snapshot_at=utc_now() + timedelta(minutes=1))
        assert wide.at(SIM_ASOF).bars(iid, Interval.DAY_1)[-1].close == Decimal("999")


class TestFundamentalsGoThroughTheSameDoor:
    def test_a_fact_filed_before_the_instant_is_readable(
        self, planted: tuple[Session, int, datetime]
    ) -> None:
        s, iid, snapshot = planted
        result = reader(s, snapshot).fact(iid, FY2024, source=FundamentalSource.SEC)

        assert result.fact is not None
        assert result.fact.value == Decimal("500000000")

    def test_the_same_fact_is_invisible_before_it_was_filed(
        self, planted: tuple[Session, int, datetime]
    ) -> None:
        s, iid, snapshot = planted
        early = PitReader(s, data_snapshot_at=snapshot).at(datetime(2025, 1, 5, tzinfo=UTC))
        assert early.fact(iid, FY2024, source=FundamentalSource.SEC).fact is None
