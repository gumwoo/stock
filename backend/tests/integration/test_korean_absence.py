"""Absence semantics survive crossing into the Korean filing regime.

The register only licenses `NOT_YET_FILED` by *failing* to find a covering
report, so any reason it cannot find one reads as proof the company has not
filed. Two such reasons arrived with DART, and neither looks like a bug at the
call site.

`list.json` states no period. The period a report covers appears only inside
its published name — `사업보고서 (2025.12)` — so every Korean filing was stored
with a null period and matched no fiscal period ever.

And a periodic report is called 10-K in one regime and 사업보고서 in another.
A fixed list of SEC form types finds nothing in a Korean register, which is
indistinguishable, downstream, from there being nothing to find.

Live before the fix: Samsung's FY2025 사업보고서, filed 2026-03-10 and sitting
in our own register, while a lookup for an account DART does not break out
answered "no report covering this period had been filed by this date".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.models import Base, Instrument
from app.models.fundamental import FiscalPeriod, FundamentalSource
from app.repositories import filing_repo
from app.repositories import fundamental_repo as repo
from app.repositories.filing_repo import FilingRow
from app.repositories.fundamental_repo import (
    FactOutcome,
    FundamentalContext,
    FundamentalRow,
)

pytestmark = pytest.mark.integration

KRX = MarketCalendar(Market.KR)

FY2025 = FundamentalContext(
    taxonomy="dart",
    concept="ResearchAndDevelopmentExpense",
    unit="KRW",
    period_end=date(2025, 12, 31),
    period_start=date(2025, 1, 1),
)

ASOF = datetime(2026, 9, 21, tzinfo=UTC)


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
def filer(engine: object) -> Iterator[tuple[Session, int]]:
    """A Korean filer whose annual report is on file but under-tagged.

    The report exists; one account in it does not reach our value source.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.KR, name="한국부재테스트", kr_corp_code="99999997")
        s.add(inst)
        s.flush()
        iid = inst.instrument_id

        filing_repo.save_filings(
            s,
            [
                FilingRow(
                    instrument_id=iid,
                    form=form,
                    filed_at=filed,
                    period_of_report=period,
                    available_at=KRX.next_session_open(filed),
                    accession=rcept,
                    source=FundamentalSource.DART,
                )
                for form, filed, period, rcept in (
                    (
                        "사업보고서 (2024.12)",
                        date(2025, 3, 11),
                        date(2024, 12, 31),
                        "20250311000001",
                    ),
                    (
                        "사업보고서 (2025.12)",
                        date(2026, 3, 10),
                        date(2025, 12, 31),
                        "20260310002820",
                    ),
                )
            ],
        )

        # The value source reaches this era — it carries a different account
        # from the same report — so coverage is not the reason the lookup
        # comes back empty.
        repo.save_facts(
            s,
            [
                FundamentalRow(
                    instrument_id=iid,
                    taxonomy="dart",
                    concept="OperatingIncomeLoss",
                    unit="KRW",
                    period_start=date(2025, 1, 1),
                    period_end=date(2025, 12, 31),
                    fiscal_year=2025,
                    fiscal_period=FiscalPeriod.FY,
                    form="사업보고서",
                    value=Decimal("43601051000000"),
                    filed_at=date(2026, 3, 10),
                    available_at=KRX.next_session_open(date(2026, 3, 10)),
                    accession="20260310002820",
                    source=FundamentalSource.DART,
                )
            ],
        )
        s.commit()

        yield s, iid

        for table in ("fundamental", "filing", "instrument"):
            s.execute(text(f"DELETE FROM {table} WHERE instrument_id = :i"), {"i": iid})
        s.commit()


def _lookup(s: Session, iid: int, asof: datetime = ASOF) -> repo.FactLookup:
    return repo.value_as_of(s, iid, FY2025, asof=asof, source=FundamentalSource.DART)


class TestKoreanAbsence:
    def test_an_untagged_account_does_not_become_an_unfiled_report(
        self, filer: tuple[Session, int]
    ) -> None:
        """The exact live failure: the report is in our hands as we deny it."""
        s, iid = filer
        result = _lookup(s, iid)
        assert result.outcome is FactOutcome.NO_OBSERVATION_IN_SOURCE

    def test_the_reason_names_the_report_it_found(self, filer: tuple[Session, int]) -> None:
        s, iid = filer
        explanation = _lookup(s, iid).explain()
        assert "사업보고서 (2025.12)" in explanation
        assert "2026-03-10" in explanation

    def test_a_period_with_no_report_still_claims_not_yet_filed(
        self, filer: tuple[Session, int]
    ) -> None:
        """The claim must stay reachable, or the fix has merely silenced it."""
        s, iid = filer
        future = FundamentalContext(
            taxonomy="dart",
            concept="OperatingIncomeLoss",
            unit="KRW",
            period_end=date(2026, 12, 31),
            period_start=date(2026, 1, 1),
        )
        result = repo.value_as_of(s, iid, future, asof=ASOF, source=FundamentalSource.DART)
        assert result.outcome is FactOutcome.NOT_YET_FILED

    def test_asking_before_the_report_was_available_claims_nothing_stronger(
        self, filer: tuple[Session, int]
    ) -> None:
        """Ahead of the value source's reach, it degrades rather than guesses.

        On 2026-01-15 FY2025 genuinely had not been reported, so
        `NOT_YET_FILED` would happen to be true — but our value source does not
        reach that date, and a claim about the world made from plumbing that
        cannot see it is right by luck. The weakest supported claim is correct
        here even though a stronger one would also have been accurate.
        """
        s, iid = filer
        early = datetime(2026, 1, 15, tzinfo=UTC)
        assert _lookup(s, iid, early).outcome is FactOutcome.SOURCE_COVERAGE_UNAVAILABLE

    def test_a_korean_report_cannot_satisfy_a_us_period(self, filer: tuple[Session, int]) -> None:
        """Registers holding both regimes must not cross-witness."""
        s, iid = filer
        assert (
            filing_repo.covering_report_exists(
                s, iid, period_end=date(2025, 12, 31), asof=ASOF, source=FundamentalSource.SEC
            )
            is None
        )

    def test_a_source_we_cannot_read_filings_from_witnesses_nothing(
        self, filer: tuple[Session, int]
    ) -> None:
        """yfinance reports no filings, so it must never license a claim."""
        s, iid = filer
        assert (
            filing_repo.covering_report_exists(
                s, iid, period_end=date(2025, 12, 31), asof=ASOF, source=FundamentalSource.YFINANCE
            )
            is None
        )
