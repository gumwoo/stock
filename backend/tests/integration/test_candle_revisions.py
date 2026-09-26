"""Candle revisions and bitemporal reproducibility.

These tests exist because of a bug that had shipped: `ON CONFLICT DO UPDATE`
rewrote a bar's OHLCV in place while deliberately holding `ingested_at` at its
original value. The comment claimed that preserved first-seen time. It did the
opposite — it produced a row whose values arrived on one date and whose
transaction time claimed another, so a reproduce-mode filter of
`ingested_at <= data_snapshot_at` would serve a correction into a snapshot
taken before the correction existed.

`test_backfilled_correction_is_invisible_to_an_earlier_snapshot` is the
scenario in full, and is the one that would have caught it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base, Candle, Instrument, Interval, SymbolHistory
from app.repositories import candle_repo
from app.repositories.candle_repo import CandleRow

pytestmark = pytest.mark.integration

BAR_TS = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


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
def session(engine: object) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        instrument = Instrument(market="KR", name="TEST CO")
        s.add(instrument)
        s.flush()
        s.add(
            SymbolHistory(
                instrument_id=instrument.instrument_id,
                symbol="TEST",
                valid_from=datetime(2020, 1, 1, tzinfo=UTC).date(),
                source="SEED",
            )
        )
        s.commit()
        s.info["instrument_id"] = instrument.instrument_id
        yield s
        s.execute(
            text("DELETE FROM candle WHERE instrument_id = :i"), {"i": instrument.instrument_id}
        )
        s.execute(
            text("DELETE FROM symbol_history WHERE instrument_id = :i"),
            {"i": instrument.instrument_id},
        )
        s.execute(
            text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": instrument.instrument_id}
        )
        s.commit()


def bar(instrument_id: int, close: str, ts: datetime = BAR_TS) -> CandleRow:
    price = Decimal(close)
    return CandleRow(
        instrument_id=instrument_id,
        interval=Interval.DAY_1,
        ts=ts,
        # KRX sessions run 6h30m, so the bar completes at 06:30 UTC.
        available_at=ts + timedelta(hours=6, minutes=30),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1000"),
        source="TEST",
    )


def _set_ingested_at(session: Session, instrument_id: int, when: datetime) -> None:
    """Stamp every currently-unstamped row, standing in for time passing."""
    session.execute(
        text("UPDATE candle SET ingested_at = :w WHERE instrument_id = :i AND ingested_at > :w"),
        {"w": when, "i": instrument_id},
    )
    session.commit()


class TestIdempotence:
    def test_recollecting_an_unchanged_bar_writes_nothing(self, session: Session) -> None:
        iid = session.info["instrument_id"]

        assert candle_repo.save_revisions(session, [bar(iid, "100")]) == 1
        session.commit()
        assert candle_repo.save_revisions(session, [bar(iid, "100")]) == 0
        session.commit()

        assert len(candle_repo.revisions_of(session, iid, Interval.DAY_1, BAR_TS)) == 1

    def test_count_counts_bars_not_revisions(self, session: Session) -> None:
        iid = session.info["instrument_id"]
        candle_repo.save_revisions(session, [bar(iid, "100")])
        session.commit()
        candle_repo.save_revisions(session, [bar(iid, "101")])
        session.commit()

        assert candle_repo.count_for(session, iid, Interval.DAY_1) == 1


class TestRevisions:
    def test_a_restated_bar_appends_rather_than_overwrites(self, session: Session) -> None:
        iid = session.info["instrument_id"]

        candle_repo.save_revisions(session, [bar(iid, "100")])
        session.commit()
        assert candle_repo.save_revisions(session, [bar(iid, "101")]) == 1
        session.commit()

        revisions = candle_repo.revisions_of(session, iid, Interval.DAY_1, BAR_TS)
        assert [str(r.close) for r in revisions] == ["100.000000", "101.000000"]

    def test_reads_return_the_newest_revision(self, session: Session) -> None:
        iid = session.info["instrument_id"]
        candle_repo.save_revisions(session, [bar(iid, "100")])
        session.commit()
        candle_repo.save_revisions(session, [bar(iid, "101")])
        session.commit()

        bars = candle_repo.history(session, iid, Interval.DAY_1)

        assert len(bars) == 1
        assert bars[0].close == Decimal("101.000000")

    def test_original_row_is_left_intact(self, session: Session) -> None:
        """The old value survives, so a restatement can be inspected."""
        iid = session.info["instrument_id"]
        candle_repo.save_revisions(session, [bar(iid, "100")])
        session.commit()
        candle_repo.save_revisions(session, [bar(iid, "101")])
        session.commit()

        originals = [
            r
            for r in session.query(Candle).filter(Candle.instrument_id == iid).all()
            if r.close == Decimal("100.000000")
        ]
        assert len(originals) == 1


class TestBitemporalReproducibility:
    def test_backfilled_correction_is_invisible_to_an_earlier_snapshot(
        self, session: Session
    ) -> None:
        """The scenario the old in-place update got wrong.

        On 9/1 the bar closed at 100 and we stored it. On 9/10 the provider
        restated it to 101. A backtest reproduced with a 9/5 data snapshot must
        still see 100, because 101 did not exist in our database on 9/5 — even
        though the bar it corrects is dated earlier still.
        """
        iid = session.info["instrument_id"]
        sep_1 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        sep_5 = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
        sep_10 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

        candle_repo.save_revisions(session, [bar(iid, "100")])
        session.commit()
        _set_ingested_at(session, iid, sep_1)

        candle_repo.save_revisions(session, [bar(iid, "101")])
        session.commit()
        _set_ingested_at(session, iid, sep_10)

        as_of_sep_5 = candle_repo.history(session, iid, Interval.DAY_1, ingested_before=sep_5)
        today = candle_repo.history(session, iid, Interval.DAY_1)

        assert as_of_sep_5[0].close == Decimal("100.000000"), (
            "a snapshot taken before the correction must not see the correction"
        )
        assert today[0].close == Decimal("101.000000")

    def test_snapshot_before_any_data_returns_nothing(self, session: Session) -> None:
        iid = session.info["instrument_id"]
        candle_repo.save_revisions(session, [bar(iid, "100")])
        session.commit()
        _set_ingested_at(session, iid, datetime(2026, 9, 1, tzinfo=UTC))

        earlier = candle_repo.history(
            session, iid, Interval.DAY_1, ingested_before=datetime(2026, 8, 1, tzinfo=UTC)
        )
        assert earlier == []

    def test_reproduced_history_is_stable_across_later_collection(self, session: Session) -> None:
        """Collecting more data must not change what an old snapshot shows."""
        iid = session.info["instrument_id"]
        snapshot = datetime(2026, 9, 5, tzinfo=UTC)

        candle_repo.save_revisions(session, [bar(iid, "100")])
        session.commit()
        _set_ingested_at(session, iid, datetime(2026, 9, 1, tzinfo=UTC))

        before = [
            str(b.close)
            for b in candle_repo.history(session, iid, Interval.DAY_1, ingested_before=snapshot)
        ]

        for close, ts in (("101", BAR_TS), ("200", BAR_TS + timedelta(days=1))):
            candle_repo.save_revisions(session, [bar(iid, close, ts)])
            session.commit()
        _set_ingested_at(session, iid, datetime(2026, 9, 20, tzinfo=UTC))

        after = [
            str(b.close)
            for b in candle_repo.history(session, iid, Interval.DAY_1, ingested_before=snapshot)
        ]

        assert before == after == ["100.000000"]


class TestBarAvailability:
    """A bar is not knowable while it is still open.

    `ts` is when the bar started; `available_at` is when it finished. A daily
    bar carries a close, a high, a low and a volume, none of which exist until
    the session ends. Filtering a simulation on `ts` would let a decision made
    at 10:00 read that day's closing price — the same look-ahead the three
    signal clocks exist to prevent, arriving through a different door.
    """

    def test_a_bar_is_not_available_while_it_is_open(self, session: Session) -> None:
        iid = session.info["instrument_id"]
        candle_repo.save_revisions(session, [bar(iid, "100")])
        session.commit()

        # 03:00 UTC: the session opened at 00:00 and closes at 06:30.
        mid_session = BAR_TS + timedelta(hours=3)

        visible = candle_repo.history(session, iid, Interval.DAY_1, available_before=mid_session)

        assert visible == [], "the day's close cannot be read while the day is still trading"

    def test_the_bar_becomes_available_at_the_close(self, session: Session) -> None:
        iid = session.info["instrument_id"]
        candle_repo.save_revisions(session, [bar(iid, "100")])
        session.commit()

        at_close = BAR_TS + timedelta(hours=6, minutes=30)

        visible = candle_repo.history(session, iid, Interval.DAY_1, available_before=at_close)

        assert len(visible) == 1
        assert visible[0].close == Decimal("100.000000")

    def test_filtering_on_ts_would_have_leaked_it(self, session: Session) -> None:
        """Names the bug this column prevents, so the distinction stays visible."""
        iid = session.info["instrument_id"]
        candle_repo.save_revisions(session, [bar(iid, "100")])
        session.commit()

        mid_session = BAR_TS + timedelta(hours=3)

        by_bar_start = candle_repo.history(session, iid, Interval.DAY_1, until=mid_session)
        by_availability = candle_repo.history(
            session, iid, Interval.DAY_1, available_before=mid_session
        )

        assert len(by_bar_start) == 1, "the bar had already opened"
        assert by_availability == [], "but it had not finished, so it was not knowable"

    def test_yesterdays_bar_is_available_during_todays_session(self, session: Session) -> None:
        """The filter must not be so strict that it hides completed history."""
        iid = session.info["instrument_id"]
        candle_repo.save_revisions(
            session, [bar(iid, "100"), bar(iid, "110", BAR_TS + timedelta(days=1))]
        )
        session.commit()

        during_the_next_session = BAR_TS + timedelta(days=1, hours=3)

        visible = candle_repo.history(
            session, iid, Interval.DAY_1, available_before=during_the_next_session
        )

        assert len(visible) == 1
        assert visible[0].close == Decimal("100.000000")


class TestNaNRevisions:
    """yfinance가 전에 정상 가격을 준 날을 나중에 NaN으로 주면(J&J 2026-08-25), NaN 수정본은 없는 것으로 본다."""

    def test_a_nan_revision_does_not_hide_the_real_one(self, session: Session) -> None:
        iid = session.info["instrument_id"]
        candle_repo.save_revisions(session, [bar(iid, "100")])
        session.commit()
        _set_ingested_at(session, iid, datetime(2026, 9, 22, tzinfo=UTC))
        candle_repo.save_revisions(session, [bar(iid, "NaN")])
        session.commit()

        assert len(candle_repo.revisions_of(session, iid, Interval.DAY_1, BAR_TS)) == 2
        got = candle_repo.history(session, iid, Interval.DAY_1)
        assert [str(c.close) for c in got] == ["100.000000"]
        assert candle_repo.opening_price(session, iid, Interval.DAY_1, BAR_TS) == Decimal("100")

    def test_a_bar_that_only_ever_had_nan_is_missing(self, session: Session) -> None:
        iid = session.info["instrument_id"]
        candle_repo.save_revisions(session, [bar(iid, "NaN")])
        session.commit()

        assert candle_repo.history(session, iid, Interval.DAY_1) == []
        assert candle_repo.opening_price(session, iid, Interval.DAY_1, BAR_TS) is None

    def test_a_bar_with_only_its_close_missing_is_missing(self, session: Session) -> None:
        iid = session.info["instrument_id"]
        row = bar(iid, "100")
        row["close"] = Decimal("NaN")
        candle_repo.save_revisions(session, [row])
        session.commit()

        assert candle_repo.history(session, iid, Interval.DAY_1) == []
        assert candle_repo.count_for(session, iid, Interval.DAY_1) == 0
