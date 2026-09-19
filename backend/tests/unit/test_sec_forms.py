"""Which SEC forms the collector accepts.

Amendments were originally excluded, which looked tidy and was wrong. A
restatement often becomes public in an amendment first: Apple restated FY2008
basic EPS from 5.48 to 6.94 in a 10-K/A filed 2010-01-25, and excluding that
form pushed the restatement's apparent publication out to the next annual 10-K
on 2010-10-27 — nine months late.

For a system whose whole claim is knowing what the market knew when, that is
the worst shape of error: quiet, and in the direction of overconfidence.
"""

from __future__ import annotations

import pytest

from app.collectors.sec_edgar import WANTED_FORMS


class TestAmendmentsAreCollected:
    @pytest.mark.parametrize("form", ["10-K/A", "10-Q/A", "20-F/A", "40-F/A"])
    def test_amended_periodic_reports_are_accepted(self, form: str) -> None:
        assert form in WANTED_FORMS

    @pytest.mark.parametrize("form", ["10-K", "10-Q", "20-F", "40-F"])
    def test_original_periodic_reports_are_accepted(self, form: str) -> None:
        assert form in WANTED_FORMS

    def test_every_accepted_form_has_its_amendment(self) -> None:
        """Otherwise a restatement in one report type would still be missed."""
        originals = {f for f in WANTED_FORMS if not f.endswith("/A")}
        for form in originals:
            assert f"{form}/A" in WANTED_FORMS, f"{form} is accepted but {form}/A is not"


class TestIrregularFormsAreExcluded:
    @pytest.mark.parametrize("form", ["8-K", "8-K/A", "S-1", "DEF 14A", "4"])
    def test_non_periodic_filings_are_not_collected(self, form: str) -> None:
        """8-K carries real numbers but irregularly.

        Mixing it into a periodic series makes period-over-period comparison
        meaningless, so it is excluded deliberately rather than by oversight.
        """
        assert form not in WANTED_FORMS
