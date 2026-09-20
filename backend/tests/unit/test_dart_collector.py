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
    _parse_amount,
    filed_date_from_receipt,
    fiscal_period_bounds,
)


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
        """Labels vary between filers; the IFRS ids do not."""
        for account_id in ACCOUNT_MAP:
            assert account_id.startswith(("ifrs-full_", "dart_")), account_id

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
