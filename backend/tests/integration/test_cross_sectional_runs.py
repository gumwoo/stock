"""A peer group under point-in-time rules, and as a coordinate of a run.

Ranking a ratio against its market means a score now depends on rows belonging
to other instruments. That opens two ways to be wrong that a single-instrument
run could not be.

The first is a leak. Nine companies' financials rebuilt on every session is
unaffordable, so the peers are cached against the instants their filings became
available - and a cache is exactly where a point-in-time bound gets lost. These
tests read the same reader at two instants and require the population to change
only where a filing actually landed, and require a fact ingested after the
run's snapshot to stay invisible no matter when it claims to have been filed.

The second is a coordinate that is not recorded. The same rule over the same
filings produces different scores against a different set of peers, so a run
that stored everything except its universe would stop reproducing the first
time the watchlist grew, and nothing in the row would explain the move.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtest.pit_repository import snapshot_now
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.types import Interval
from app.engines.fundamental import REQUIRED_MONTHS
from app.models import Base, Instrument
from app.models.fundamental import FiscalPeriod, FundamentalSource
from app.repositories import candle_repo, fundamental_repo
from app.repositories.candle_repo import CandleRow
from app.services import backtest_service as svc
from app.services import fundamental_service
from tests.conftest import fake_cik

pytestmark = pytest.mark.integration

BASE_CIK = fake_cik(__name__)
US = MarketCalendar(Market.US)
HISTORY = US.sessions_between(date(2023, 1, 3), date(2024, 12, 31))

# Six companies, so a population clears the default `min_peers` of five.
MEMBERS = 6

# Each company earns a different return on the same equity, so a rank over the
# group is an ordering rather than a tie.
EQUITY = Decimal("500000000")
INCOMES = [Decimal(n) for n in (10_000_000, 30_000_000, 50_000_000, 70_000_000, 90_000_000)]
MOVER_BEFORE = Decimal("20000000")
MOVER_AFTER = Decimal("200000000")


def _cik(index: int) -> str:
    return str(int(BASE_CIK) + index).zfill(len(BASE_CIK))


def _candles(iid: int) -> list[CandleRow]:
    return [
        CandleRow(
            instrument_id=iid,
            interval=Interval.DAY_1,
            ts=US.session_open(day),
            available_at=US.session_close(day),
            open=Decimal(100 + i),
            high=Decimal(100 + i),
            low=Decimal(100 + i),
            close=Decimal(100 + i),
            volume=Decimal("1000"),
            source="TEST",
        )
        for i, day in enumerate(HISTORY)
    ]


def _facts(
    iid: int, *, year: int, income: Decimal, accession: str
) -> list[fundamental_repo.FundamentalRow]:
    """One annual filing, complete enough for the engine to score it."""
    figures: dict[str, Decimal] = {
        "Revenues": Decimal("400000000"),
        "NetIncomeLoss": income,
        "OperatingIncomeLoss": Decimal("110000000"),
        "EarningsPerShareBasic": Decimal("5.5"),
        "EarningsPerShareDiluted": Decimal("5.4"),
        "Assets": Decimal("800000000"),
        "Liabilities": Decimal("300000000"),
        "StockholdersEquity": EQUITY,
        "CashAndCashEquivalentsAtCarryingValue": Decimal("120000000"),
    }
    filed = date(year + 1, 2, 1)
    return [
        fundamental_repo.FundamentalRow(
            instrument_id=iid,
            taxonomy="us-gaap",
            concept=concept,
            unit=fundamental_service.unit_for(concept, "USD"),
            period_start=None if REQUIRED_MONTHS[concept] is None else date(year, 1, 1),
            period_end=date(year, 12, 31),
            fiscal_year=year,
            fiscal_period=FiscalPeriod.FY,
            form="10-K",
            value=value,
            filed_at=filed,
            available_at=US.next_session_open(filed),
            accession=accession,
            source=FundamentalSource.SEC,
        )
        for concept, value in figures.items()
    ]


@pytest.fixture(scope="module")
def db() -> Iterator[object]:
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
def market(db: object) -> Iterator[tuple[Session, list[Instrument]]]:
    """Six listed companies. The first is the subject; the last one moves.

    The mover files FY2022 with a poor result and FY2023 with a strong one, so
    a population read before and after that second filing must differ - and
    must differ only then.
    """
    factory = sessionmaker(bind=db, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        made: list[Instrument] = []
        for index in range(MEMBERS):
            # Tracked, because a peer group is drawn from tracked rows only:
            # a name-only listing has no financials to rank.
            inst = Instrument(
                market=Market.US, name=f"PEER {index} CORP", us_cik=_cik(index), tracked=True
            )
            s.add(inst)
            s.flush()
            made.append(inst)
            candle_repo.save_revisions(s, _candles(inst.instrument_id))

        for index, inst in enumerate(made[:-1]):
            fundamental_repo.save_facts(
                s,
                _facts(
                    inst.instrument_id,
                    year=2022,
                    income=INCOMES[index],
                    accession=f"{_cik(index)}-2022-FY",
                ),
            )

        mover = made[-1]
        for year, income in ((2022, MOVER_BEFORE), (2023, MOVER_AFTER)):
            fundamental_repo.save_facts(
                s,
                _facts(
                    mover.instrument_id,
                    year=year,
                    income=income,
                    accession=f"{_cik(MEMBERS - 1)}-{year}-FY",
                ),
            )
        s.commit()

        yield s, made

        ids = [i.instrument_id for i in made]
        s.execute(text("DELETE FROM backtest_run WHERE instrument_id = ANY(:ids)"), {"ids": ids})
        for table in ("fundamental", "candle", "instrument"):
            s.execute(text(f"DELETE FROM {table} WHERE instrument_id = ANY(:ids)"), {"ids": ids})
        s.commit()


def _universe(made: list[Instrument]) -> tuple[int, ...]:
    return tuple(sorted(i.instrument_id for i in made))


class TestPeersObeyThePointInTimeBound:
    def test_a_population_only_moves_when_a_filing_lands(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """Read before and after the mover's second filing.

        Everything the cache does rests on one claim: between two consecutive
        availability instants the same rows qualify, so the same snapshot is
        the right answer throughout. If that were false the population would
        drift across sessions where nothing was filed, and the drift would
        look like the market changing.
        """
        s, made = market
        reader = svc.reader_for(s, made[0], snapshot_now(s), universe=_universe(made))

        early = reader.at(US.session_close(date(2023, 6, 1))).peers()
        just_before = reader.at(US.session_close(date(2024, 1, 31))).peers()
        after = reader.at(US.session_close(date(2024, 6, 3))).peers()

        assert early is not None and just_before is not None and after is not None
        assert early.population("ROE") == just_before.population("ROE")
        assert after.population("ROE") != early.population("ROE")

        best_before = max(early.population("ROE"))
        best_after = max(after.population("ROE"))
        assert best_after > best_before

    def test_nothing_is_visible_before_it_was_filed(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """The mover's FY2023 result is absent from a 2023 population."""
        s, made = market
        reader = svc.reader_for(s, made[0], snapshot_now(s), universe=_universe(made))

        early = reader.at(US.session_close(date(2023, 6, 1))).peers()

        assert early is not None
        strong = float(MOVER_AFTER) / float(EQUITY)
        assert all(roe < strong for roe in early.population("ROE"))

    def test_a_later_ingest_stays_invisible_under_an_earlier_snapshot(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """A backfill carries an old filing date and must not reach a past run.

        This is the failure `available_at` alone cannot catch. The row claims
        to have been filed in 2022, so an availability filter admits it; only
        the ingest bound knows the run never had it.

        The commits are load-bearing, and the first draft of this test passed
        a leak through without them. Postgres `now()` is the transaction's
        start time, not the wall clock, so a snapshot taken and a backfill
        written inside one transaction get the same instant and the row lands
        exactly on the boundary rather than after it. Each step here is its
        own transaction, which is also how the two actually happen: a
        collector writes long after a run read.
        """
        s, made = market
        subject, extra = made[0], made[1]

        s.commit()
        before_backfill = snapshot_now(s)

        baseline = (
            svc.reader_for(s, subject, before_backfill, universe=_universe(made))
            .at(US.session_close(date(2024, 6, 3)))
            .peers()
        )
        assert baseline is not None
        s.commit()

        fundamental_repo.save_facts(
            s,
            _facts(
                extra.instrument_id,
                year=2023,
                income=Decimal("480000000"),
                accession=f"{_cik(1)}-2023-BACKFILL",
            ),
        )
        s.commit()

        unchanged = (
            svc.reader_for(s, subject, before_backfill, universe=_universe(made))
            .at(US.session_close(date(2024, 6, 3)))
            .peers()
        )
        widened = (
            svc.reader_for(s, subject, snapshot_now(s), universe=_universe(made))
            .at(US.session_close(date(2024, 6, 3)))
            .peers()
        )

        assert unchanged is not None and widened is not None
        assert unchanged.population("ROE") == baseline.population("ROE")
        assert widened.population("ROE") != baseline.population("ROE")


class TestTheGroupReachesTheScore:
    def test_a_ranked_metric_names_the_population_it_used(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """The wiring holds all the way from the reader to a stored metric."""
        from app.core.types import DataProvenance, Freshness
        from app.engines.fundamental import FundamentalEngine

        s, made = market
        asof = US.session_close(date(2024, 6, 3))
        reader = svc.reader_for(s, made[0], snapshot_now(s), universe=_universe(made)).at(asof)

        snapshot = reader.fundamentals(made[0].instrument_id, price=150.0, currency="USD")
        factor, _ = FundamentalEngine().evaluate(
            snapshot,
            requested_weight=0.4,
            provenance=DataProvenance(
                source_asof=asof,
                source_checked_at=asof,
                data_age=asof - asof,
                freshness=Freshness.FRESH,
            ),
            peers=reader.peers(),
        )

        roe = next(m for m in factor.metrics if m.name == "ROE")
        assert roe.detail is not None
        assert f"비교군 {MEMBERS}개 중 순위" in roe.detail

    def test_asking_for_peers_counts_as_reading_financials(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """Otherwise the coverage gate would skip a run that ranked."""
        s, made = market
        reader = svc.reader_for(s, made[0], snapshot_now(s), universe=_universe(made))
        assert not reader.read_fundamentals

        reader.at(US.session_close(date(2024, 6, 3))).peers()

        assert reader.read_fundamentals

    def test_no_universe_means_no_population(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        s, made = market
        reader = svc.reader_for(s, made[0], snapshot_now(s))

        assert reader.at(US.session_close(date(2024, 6, 3))).peers() is None
        assert not reader.read_fundamentals


class TestTheUniverseIsARunCoordinate:
    def test_market_universe_is_a_sorted_set_of_the_same_market(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        s, made = market
        group = svc.market_universe(s, made[0], asof=date(2026, 9, 22))

        assert list(group) == sorted(set(group))
        assert made[0].instrument_id in group

    def test_a_stored_run_records_the_group_it_ranked_within(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """A row that omitted this could not explain its own numbers."""
        s, made = market
        universe = _universe(made)
        run = _persisted(s, made[0], universe=universe)

        assert run.universe == list(universe)

    def test_a_run_with_no_universe_records_null_rather_than_empty(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """NULL is a state that happened; an empty list is one that did not.

        Every run stored before ranking existed scored on the fixed scale, and
        NULL says exactly that. Writing `[]` would claim a peer group was
        assembled and came back empty, which no code path produces.
        """
        s, made = market
        run = _persisted(s, made[1], universe=None)

        assert run.universe is None

    def test_a_holdout_from_a_differently_scoped_report_is_refused(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """The peer group is compared like every other coordinate.

        `experiment_fields` is the one list used for storing a run and for
        checking a later holdout belongs to it, so a coordinate added there is
        checked from the moment it is written rather than the next time
        somebody remembers to.
        """
        s, made = market
        universe = _universe(made)
        run = _persisted(s, made[0], universe=universe)

        narrower = _report(s, made[0], universe=universe[:-1])

        with pytest.raises(svc.HoldoutError, match="universe"):
            svc.evaluate_and_persist_holdout(s, run, narrower)


class TestIntegrityReadsTheGroup:
    def test_a_universe_missing_its_own_instrument_is_a_finding(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """Ranked among companies it is not part of, a score describes nothing."""
        from app.services import reproduce_service as rs

        s, made = market
        stored = _persisted(s, made[0], universe=_universe(made))
        windows = svc.backtest_repo.windows_of(s, stored.id)
        run = _detached(s, stored.id)

        run.universe = [i for i in run.universe or [] if i != made[0].instrument_id]
        findings = rs.check_integrity(run, windows)

        assert any("does not contain instrument" in f for f in findings)

    def test_an_unsorted_universe_is_a_finding(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        """Only `market_universe` should ever have written this column."""
        from app.services import reproduce_service as rs

        s, made = market
        stored = _persisted(s, made[0], universe=_universe(made))
        windows = svc.backtest_repo.windows_of(s, stored.id)
        run = _detached(s, stored.id)

        run.universe = list(reversed(run.universe or []))
        findings = rs.check_integrity(run, windows)

        assert any("not a sorted set" in f for f in findings)

    def test_an_untouched_run_has_nothing_to_report(
        self, market: tuple[Session, list[Instrument]]
    ) -> None:
        from app.services import reproduce_service as rs

        s, made = market
        stored = _persisted(s, made[0], universe=_universe(made))
        windows = svc.backtest_repo.windows_of(s, stored.id)

        assert rs.check_integrity(stored, windows) == ()


def _report(
    s: Session, instrument: Instrument, *, universe: tuple[int, ...] | None
) -> svc.WalkForwardReport:
    """A short walk-forward over the synthetic history.

    Buy-and-hold rather than the scoring rule: these tests are about the
    coordinate being carried, and a harness keeps them from also depending on
    what the rule decides on invented prices.
    """
    from app.backtest.strategies import buy_and_hold

    return svc.walk_forward(
        s,
        svc.StrategySpec(definition=buy_and_hold()),
        svc.RunRequest(
            instrument_id=instrument.instrument_id,
            start=HISTORY[0],
            end=HISTORY[-1],
            universe=universe,
        ),
        train_sessions=120,
        eval_sessions=60,
        holdout_sessions=60,
        require_complete_sessions=False,
    )


def _persisted(s: Session, instrument: Instrument, *, universe: tuple[int, ...] | None) -> object:
    run = svc.persist(s, _report(s, instrument, universe=universe))
    s.commit()
    return run


def _detached(s: Session, run_id: int) -> object:
    """A stored run to read, with no way back to the database.

    The integrity probes below corrupt a coordinate to check it is noticed.
    Left attached, that edit is a pending UPDATE the session will try to flush
    against a row the teardown has already deleted, and the fixture fails on
    the way out instead of the test failing on the way in.
    """
    run = svc.backtest_repo.get_run(s, run_id)
    assert run is not None
    s.expunge(run)
    return run
