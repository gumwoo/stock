"""DART-specific parsing, and the one genuinely fragile step in it.

DART states no period dates. A statement row says `제 57 기` and nothing more,
so the fiscal span is reconstructed from the business year and the company's
`acc_mt` — its fiscal year-end month. Everything downstream depends on that
reconstruction, and getting it wrong would shift every Korean period silently,
so it is isolated in one function and tested directly, including the filers who
do not close in December.

The other DART shape worth pinning is that one response carries three years.
Each row holds `thstrm`, `frmtrm` and `bfefrmtrm` under a single receipt
number, which is what gives Korean filings the same restatement history SEC
comparatives provide.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.collectors.dart_fundamental import (
    ACCOUNT_MAP,
    INSTANTANEOUS,
    PER_SHARE,
    DartFundamentalCollector,
    _parse_amount,
    filed_date_from_receipt,
    fiscal_period_bounds,
    report_period_end,
)
from app.core.calendar import Market, MarketCalendar


class TestFiscalPeriodBounds:
    def test_a_december_filer_runs_the_calendar_year(self) -> None:
        assert fiscal_period_bounds(2025, 12) == (date(2025, 1, 1), date(2025, 12, 31))

    def test_a_march_filer_straddles_two_calendar_years(self) -> None:
        """Assuming December would shift this filer's every period by nine months."""
        assert fiscal_period_bounds(2025, 3) == (date(2024, 4, 1), date(2025, 3, 31))

    @pytest.mark.parametrize(
        ("month", "expected_end"),
        [
            (1, date(2025, 1, 31)),
            (2, date(2025, 2, 28)),
            (6, date(2025, 6, 30)),
            (9, date(2025, 9, 30)),
            (11, date(2025, 11, 30)),
        ],
    )
    def test_the_end_is_the_last_day_of_the_month(self, month: int, expected_end: date) -> None:
        _, end = fiscal_period_bounds(2025, month)
        assert end == expected_end

    def test_february_in_a_leap_year(self) -> None:
        assert fiscal_period_bounds(2024, 2)[1] == date(2024, 2, 29)

    def test_the_span_is_one_year(self) -> None:
        for month in range(1, 13):
            start, end = fiscal_period_bounds(2025, month)
            assert 360 <= (end - start).days <= 371, f"month {month} spans oddly"

    def test_consecutive_years_do_not_overlap(self) -> None:
        _, first_end = fiscal_period_bounds(2024, 3)
        second_start, _ = fiscal_period_bounds(2025, 3)
        assert second_start > first_end

    @pytest.mark.parametrize("month", [0, 13, -1])
    def test_an_impossible_month_raises(self, month: int) -> None:
        """Fail loudly rather than deriving a plausible wrong period."""
        with pytest.raises(ValueError, match="must be 1-12"):
            fiscal_period_bounds(2025, month)


class TestReceiptNumbers:
    def test_the_filing_date_is_the_first_eight_digits(self) -> None:
        assert filed_date_from_receipt("20260310002820") == date(2026, 3, 10)

    def test_a_malformed_receipt_yields_nothing(self) -> None:
        for bad in ("", "2026", "abcdefgh0001", "20261340000001"):
            assert filed_date_from_receipt(bad) is None


class TestAmountParsing:
    def test_plain_digits(self) -> None:
        assert _parse_amount("333605938000000") == Decimal("333605938000000")

    def test_comma_grouping_is_stripped(self) -> None:
        assert _parse_amount("333,605,938,000,000") == Decimal("333605938000000")

    def test_negatives_survive(self) -> None:
        assert _parse_amount("-1234000") == Decimal("-1234000")

    @pytest.mark.parametrize("blank", ["", "  ", "-", None])
    def test_unreported_values_are_absent_not_zero(self, blank: object) -> None:
        """DART leaves a field blank when an account was not reported.

        Reading that as zero would turn a missing figure into a claim about the
        business — the same mistake as scoring absence.
        """
        assert _parse_amount(blank) is None

    def test_nonsense_is_absent_rather_than_raising(self) -> None:
        assert _parse_amount("N/A") is None


class TestAccountMapping:
    def test_concepts_match_the_names_the_engine_speaks(self) -> None:
        """A Korean filing and a US one must land in the same vocabulary."""
        from app.engines.fundamental import REQUIRED_MONTHS

        for concept in ACCOUNT_MAP.values():
            assert concept in REQUIRED_MONTHS, f"{concept} is not a concept the engine reads"

    def test_mapping_is_on_taxonomy_ids_not_korean_labels(self) -> None:
        """Labels vary between filers; the taxonomy ids do not.

        Three namespaces, not two. IFRS renamed its prefix from `ifrs` to
        `ifrs-full` with the 2018 edition and DART returns whichever the filing
        used, so both spellings are legitimate ids — see
        `TestBothIfrsNamespaces` for why that matters.
        """
        for account_id in ACCOUNT_MAP:
            assert account_id.startswith(("ifrs-full_", "ifrs_", "dart_")), account_id

    def test_every_ifrs_concept_is_mapped_under_both_prefixes(self) -> None:
        """Adding one spelling and forgetting the other is the original bug."""
        for account_id, concept in list(ACCOUNT_MAP.items()):
            if not account_id.startswith("ifrs"):
                continue
            local = account_id.split("_", 1)[1]
            assert ACCOUNT_MAP.get(f"ifrs_{local}") == concept
            assert ACCOUNT_MAP.get(f"ifrs-full_{local}") == concept

    def test_balances_are_marked_instantaneous(self) -> None:
        """So a balance is never given a span and divided by a flow."""
        assert "Assets" in INSTANTANEOUS
        assert "StockholdersEquity" in INSTANTANEOUS
        assert "Revenues" not in INSTANTANEOUS
        assert "NetIncomeLoss" not in INSTANTANEOUS

    def test_per_share_concepts_are_flagged(self) -> None:
        """Their unit is KRW/shares, which must not collide with KRW."""
        assert "EarningsPerShareBasic" in PER_SHARE
        assert "Revenues" not in PER_SHARE

    def test_every_instantaneous_concept_is_mapped(self) -> None:
        for concept in INSTANTANEOUS:
            assert concept in ACCOUNT_MAP.values()


class TestReportPeriods:
    """DART states a report's period only inside its own name.

    `list.json` has no period field, so a register built without parsing the
    name matches no fiscal period ever — and a failed match is what the
    absence logic reads as proof the company has not filed.
    """

    @pytest.mark.parametrize(
        ("report_nm", "expected"),
        [
            ("사업보고서 (2025.12)", date(2025, 12, 31)),
            ("반기보고서 (2026.06)", date(2026, 6, 30)),
            ("분기보고서 (2024.03)", date(2024, 3, 31)),
            ("사업보고서 (2024.02)", date(2024, 2, 29)),
        ],
    )
    def test_the_period_is_the_last_day_of_the_stated_month(
        self, report_nm: str, expected: date
    ) -> None:
        assert report_period_end(report_nm) == expected

    def test_a_correction_still_states_its_period(self) -> None:
        assert report_period_end("[기재정정]분기보고서 (2024.03)") == date(2024, 3, 31)

    @pytest.mark.parametrize("report_nm", ["감사보고서제출", "사업보고서", "사업보고서 (2025.13)"])
    def test_a_name_without_a_usable_period_yields_nothing(self, report_nm: str) -> None:
        """Null is honest here: such a filing simply never matches a period."""
        assert report_period_end(report_nm) is None


class TestFormFamilies:
    """A periodic report is called 10-K in one regime and 사업보고서 in another."""

    def test_korean_report_kinds_are_recognised(self) -> None:
        from app.models.fundamental import FundamentalSource
        from app.repositories.filing_repo import FORM_FAMILIES, form_family

        korean = FORM_FAMILIES[FundamentalSource.DART]
        for name in ("사업보고서 (2025.12)", "반기보고서 (2026.06)", "분기보고서 (2024.03)"):
            assert form_family(name) in korean

    def test_a_correction_belongs_to_the_family_it_corrects(self) -> None:
        """As 10-K/A does: an amendment is not a different kind of report."""
        from app.repositories.filing_repo import form_family

        assert form_family("[기재정정]사업보고서 (2025.12)") == "사업보고서"
        assert form_family("10-K/A") == "10-K"

    def test_the_period_never_leaks_into_the_family(self) -> None:
        """Otherwise every fiscal period becomes its own kind of report."""
        from app.repositories.filing_repo import form_family

        assert form_family("사업보고서 (2025.12)") == form_family("사업보고서 (2024.12)")

    def test_a_source_without_filings_has_an_empty_family_list(self) -> None:
        from app.models.fundamental import FundamentalSource
        from app.repositories.filing_repo import FORM_FAMILIES

        assert FORM_FAMILIES[FundamentalSource.YFINANCE] == ()


class TestFilingPagination:
    """`list.json` caps a page at 100 and reports how many pages exist.

    Reading only the first page does not merely lose filings — the ones it
    drops are the oldest, so the register appears to begin later than it does
    and declines to speak about periods it could have witnessed. Samsung's
    periodic disclosures since 1999 came back as exactly 100 rows, which is
    what that truncation looks like from the outside.
    """

    def _collector_over(self, pages: list[list[dict[str, str]]]) -> object:
        from app.collectors.dart_fundamental import DartFundamentalCollector

        collector = DartFundamentalCollector()
        requested: list[str] = []

        def fake_get(client: object, path: str, **params: str) -> dict[str, object]:
            requested.append(params.get("page_no", "1"))
            index = int(params.get("page_no", "1")) - 1
            return {"status": "000", "list": pages[index], "total_page": len(pages)}

        collector._get = fake_get  # type: ignore[assignment,method-assign]
        collector.requested = requested  # type: ignore[attr-defined]
        return collector

    @staticmethod
    def _page(start: int, count: int) -> list[dict[str, str]]:
        return [
            {
                "rcept_no": f"2020{i % 12 + 1:02d}10{i:06d}",
                "report_nm": f"분기보고서 (2020.{i % 12 + 1:02d})",
            }
            for i in range(start, start + count)
        ]

    def test_every_page_is_read(self) -> None:
        from app.core.calendar import Market, MarketCalendar

        pages = [self._page(0, 100), self._page(100, 14)]
        collector = self._collector_over(pages)
        rows = collector._collect_filings(  # type: ignore[attr-defined]
            None, "00126380", 1, MarketCalendar(Market.KR)
        )

        assert len(rows) == 114
        assert collector.requested == ["1", "2"]  # type: ignore[attr-defined]

    def test_a_single_page_does_not_ask_for_another(self) -> None:
        from app.core.calendar import Market, MarketCalendar

        collector = self._collector_over([self._page(0, 7)])
        rows = collector._collect_filings(  # type: ignore[attr-defined]
            None, "00126380", 1, MarketCalendar(Market.KR)
        )

        assert len(rows) == 7
        assert collector.requested == ["1"]  # type: ignore[attr-defined]

    def test_paged_filings_carry_their_period(self) -> None:
        from app.core.calendar import Market, MarketCalendar

        collector = self._collector_over([self._page(0, 3)])
        rows = collector._collect_filings(  # type: ignore[attr-defined]
            None, "00126380", 1, MarketCalendar(Market.KR)
        )

        assert all(r.period_of_report is not None for r in rows)


LEGACY = [
    {
        "account_id": "ifrs_Revenue",
        "rcept_no": "20160330003536",
        "currency": "KRW",
        "thstrm_amount": "200,653,482",
        "frmtrm_amount": "206,205,987",
        "bfefrmtrm_amount": "228,692,667",
    },
    {
        "account_id": "ifrs_ProfitLossAttributableToOwnersOfParent",
        "rcept_no": "20160330003536",
        "currency": "KRW",
        "thstrm_amount": "19,060,144",
        "frmtrm_amount": "23,394,358",
        "bfefrmtrm_amount": "30,474,764",
    },
    {
        "account_id": "ifrs_BasicEarningsLossPerShare",
        "rcept_no": "20160330003536",
        "currency": "KRW",
        "thstrm_amount": "126,305",
        "frmtrm_amount": "153,105",
        "bfefrmtrm_amount": "197,841",
    },
    {
        "account_id": "ifrs_Assets",
        "rcept_no": "20160330003536",
        "currency": "KRW",
        "thstrm_amount": "242,179,521",
        "frmtrm_amount": "230,422,958",
        "bfefrmtrm_amount": "214,075,018",
    },
]


class TestBothIfrsNamespaces:
    """A filing's prefix is a fact about when it was filed, not about what it says.

    IFRS moved from `ifrs` to `ifrs-full` with its 2018 taxonomy, and DART's
    full-statement endpoint returns whichever spelling the filing used. Asked
    directly for Samsung, business years 2015 through 2018 come back under
    `ifrs_` and 2019 onwards under `ifrs-full_`.

    Mapping only the newer spelling recognised one concept in nine for every
    year before 2019 — and the survivor was `dart_OperatingIncomeLoss`, a DART
    extension that never moved. So every year still returned rows. Nothing was
    empty, nothing errored, and four years of Korean fundamentals held a single
    figure that the scorer cannot anchor on, which a ten-year backtest then ran
    straight through.

    The values below are the shape DART actually returns, with the amounts
    shortened.
    """

    def rows(self, items: list[dict[str, object]], year: int = 2015) -> list[object]:
        collector = DartFundamentalCollector()
        rows, _ = collector._to_rows(
            items,  # type: ignore[arg-type]
            instrument_id=1,
            business_year=year,
            fiscal_end_month=12,
            calendar=MarketCalendar(Market.KR),
        )
        return list(rows)

    def test_legacy_ids_become_the_same_concepts(self) -> None:
        produced = {r.concept for r in self.rows(LEGACY)}  # type: ignore[attr-defined]

        assert produced == {
            "Revenues",
            "NetIncomeLoss",
            "EarningsPerShareBasic",
            "Assets",
        }

    def test_the_two_spellings_produce_identical_rows(self) -> None:
        """Only the id differs, so only the id may differ in the result."""
        modern = [
            {**item, "account_id": item["account_id"].replace("ifrs_", "ifrs-full_")}
            for item in LEGACY
        ]

        old = sorted(
            (r.concept, r.period_end, r.value, r.unit)  # type: ignore[attr-defined]
            for r in self.rows(LEGACY)
        )
        new = sorted(
            (r.concept, r.period_end, r.value, r.unit)  # type: ignore[attr-defined]
            for r in self.rows(modern)
        )

        assert old == new

    def test_the_comparative_columns_still_reach_back_two_years(self) -> None:
        """The legacy path must keep the property the whole collector rests on:
        one report carries three years."""
        ends = sorted(
            {r.period_end for r in self.rows(LEGACY)}  # type: ignore[attr-defined]
        )

        assert ends == [date(2013, 12, 31), date(2014, 12, 31), date(2015, 12, 31)]

    def test_a_prefix_we_do_not_know_is_still_ignored(self) -> None:
        """Accepting both spellings is not accepting anything that looks close."""
        unknown = [{**LEGACY[0], "account_id": "entity00126380_Revenue"}]

        assert self.rows(unknown) == []


class TestTheTotalsAreNotTheParentFigures:
    """`ifrs-full_ProfitLoss` is not `NetIncomeLoss`, and the gap is material.

    us-gaap's `NetIncomeLoss` and `StockholdersEquity` are both attributable to
    the parent; the including-noncontrolling-interests elements are
    `ProfitLoss` and
    `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`.
    The SEC collector reads the us-gaap elements directly, so a Korean row
    filled from an IFRS total would share a column with an American row and
    mean something else.

    Measured on FY2023 filings the two profit figures differ by 34.8% for
    LG화학, 8.0% for POSCO홀딩스 and 6.5% for 삼성전자, and NAVER's parent
    figure is the larger. A cross-market comparison was comparing two different
    quantities, and nothing in the stored row said so.
    """

    @pytest.mark.parametrize(
        "account_id",
        [
            "ifrs-full_ProfitLoss",
            "ifrs_ProfitLoss",
            "ifrs-full_Equity",
            "ifrs_Equity",
        ],
    )
    def test_an_including_nci_total_is_not_mapped(self, account_id: str) -> None:
        assert account_id not in ACCOUNT_MAP

    @pytest.mark.parametrize(
        ("account_id", "concept"),
        [
            ("ifrs-full_ProfitLossAttributableToOwnersOfParent", "NetIncomeLoss"),
            ("ifrs_ProfitLossAttributableToOwnersOfParent", "NetIncomeLoss"),
            ("ifrs-full_EquityAttributableToOwnersOfParent", "StockholdersEquity"),
            ("ifrs_EquityAttributableToOwnersOfParent", "StockholdersEquity"),
        ],
    )
    def test_the_parent_figure_is(self, account_id: str, concept: str) -> None:
        assert ACCOUNT_MAP[account_id] == concept

    def test_a_total_in_the_payload_is_ignored(self) -> None:
        """Not merely unmapped in the table — dropped by `_to_rows` too."""
        payload = [{**LEGACY[0], "account_id": "ifrs_ProfitLoss"}]
        collector = DartFundamentalCollector()

        rows, _ = collector._to_rows(
            payload,  # type: ignore[arg-type]
            instrument_id=1,
            business_year=2015,
            fiscal_end_month=12,
            calendar=MarketCalendar(Market.KR),
        )

        assert list(rows) == []


class TestEveryDartCallIsOnTheLedger:
    """The ledger is only a floor if every metered call reaches it.

    This collector called DART with a rate bucket and no reservation, so the
    quota report said zero while the day's allowance drained. The failure that
    followed was the listing master dying on a refusal whose message said "our
    ledger had room" — accurate, and pointing at the wrong file.
    """

    def test_a_request_reserves_before_it_is_sent(self) -> None:
        import httpx

        from app.collectors.dart_fundamental import QUOTA_GROUP, DartFundamentalCollector

        reserved: list[tuple[str, str]] = []

        class Guard:
            def reserve(
                self, group: str, endpoint: str, *, calls: int = 1, now: object = None
            ) -> None:
                reserved.append((group, endpoint))

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"status": "000", "list": []})

        c = DartFundamentalCollector(guard=Guard())  # type: ignore[arg-type]
        c._key = "test-key"
        c._bucket = type("NoWait", (), {"acquire": lambda self: None})()  # type: ignore[assignment]

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            c._get(client, "list.json", corp_code="00126380")
            c._get(client, "company.json", corp_code="00126380")

        assert len(seen) == 2
        assert reserved == [(QUOTA_GROUP, "list"), (QUOTA_GROUP, "company")]

    def test_it_shares_the_group_the_master_uses(self) -> None:
        """One published DART cap, so one ledger group."""
        from app.collectors.dart_fundamental import QUOTA_GROUP as FUNDAMENTAL_GROUP
        from app.collectors.krx_master import QUOTA_GROUP as MASTER_GROUP

        assert FUNDAMENTAL_GROUP == MASTER_GROUP


class TestDartResponseShapes:
    """The guards `krx_master` has, on the collector that calls the same API.

    Both read `list.json`; one was hardened over four review rounds and the
    other was not touched. A response that parses but is the wrong shape raises
    `AttributeError` or `TypeError`, neither of which is a `CollectorError`, so
    `run_collector` re-raises and files an outage as a defect in our own code.
    """

    @staticmethod
    def answering(body: object) -> tuple[DartFundamentalCollector, object]:
        import httpx

        class Guard:
            def reserve(
                self, group: str, endpoint: str, *, calls: int = 1, now: object = None
            ) -> None:
                return None

        c = DartFundamentalCollector(guard=Guard())  # type: ignore[arg-type]
        c._key = "test-key"
        c._bucket = type("NoWait", (), {"acquire": lambda self: None})()  # type: ignore[assignment]
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        )
        return c, client

    def test_a_payload_that_is_not_an_object_is_an_outage(self) -> None:
        from app.collectors.base import UpstreamUnavailableError

        c, client = self.answering([1, 2, 3])
        with client, pytest.raises(UpstreamUnavailableError, match="not an object"):
            c._get(client, "list.json", corp_code="00126380")  # type: ignore[arg-type]

    def test_filing_rows_that_are_not_a_list_are_an_outage(self) -> None:
        from app.collectors.base import UpstreamUnavailableError

        c, client = self.answering({"status": "000", "list": "한 건"})
        with client, pytest.raises(UpstreamUnavailableError, match="rows were expected"):
            c._collect_filings(  # type: ignore[attr-defined]
                client,
                corp_code="00126380",
                instrument_id=1,
                calendar=MarketCalendar(Market.KR),
            )

    def test_account_rows_that_are_not_a_list_are_an_outage(self) -> None:
        """The second `list.json`-shaped response, on the financial endpoint."""
        from app.collectors.base import UpstreamUnavailableError

        c, client = self.answering({"status": "000", "list": {"account_id": "x"}})
        with client, pytest.raises(UpstreamUnavailableError, match="rows were expected"):
            c._accounts(client, corp_code="00126380", year=2025)  # type: ignore[arg-type]

    def test_absent_account_rows_are_simply_empty(self) -> None:
        c, client = self.answering({"status": "000"})
        with client:
            assert c._accounts(client, corp_code="00126380", year=2025) == []  # type: ignore[arg-type]

    def test_a_filing_row_that_is_not_an_object_is_skipped(self) -> None:
        c, client = self.answering({"status": "000", "list": ["rubbish", 7]})
        with client:
            rows = c._collect_filings(  # type: ignore[attr-defined]
                client,
                corp_code="00126380",
                instrument_id=1,
                calendar=MarketCalendar(Market.KR),
            )

        assert rows == []

    def test_an_account_row_that_is_not_an_object_is_skipped(self) -> None:
        c, _client = self.answering({})
        rows, seen = c._to_rows(
            ["rubbish", 7, None],  # type: ignore[arg-type]
            instrument_id=1,
            business_year=2025,
            fiscal_end_month=12,
            calendar=MarketCalendar(Market.KR),
        )

        assert rows == []
        assert seen == 0

    def test_a_numeric_account_id_is_not_read_as_a_concept(self) -> None:
        """`str(1234)` would match nothing, but the read itself used to raise."""
        c, _client = self.answering({})
        rows, _ = c._to_rows(
            [{"account_id": 1234, "rcept_no": 20260101000001}],  # type: ignore[arg-type]
            instrument_id=1,
            business_year=2025,
            fiscal_end_month=12,
            calendar=MarketCalendar(Market.KR),
        )

        assert rows == []
