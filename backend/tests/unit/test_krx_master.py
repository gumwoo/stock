"""The Korean listing master, and what it does when DART answers with a refusal.

`corpCode.xml` is the first call every master run makes, and it is the only
DART endpoint that returns a binary archive. Its error responses are not
binary: a key that is not registered, a quota refusal or a maintenance window
all come back as an XML `<result><status>` document, at HTTP 200, in the body
where the archive was expected.

Handed to `zipfile` unchecked, each of those raises `BadZipFile`. That is not a
`CollectorError`, so `run_collector` re-raises it and records the run as
"internal error; see logs" — the status reserved for a defect in our own code.
An outage would then look exactly like a bug here, and telling those two apart
is the whole reason the error taxonomy exists.

The sibling endpoints already classify status `020` as a rate limit. This one
is the one that did not.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta
from itertools import pairwise

import pytest

from app.collectors.base import CollectorError, RateLimitedError, UpstreamUnavailableError
from app.collectors.krx_master import KrxMasterCollector


def archive(*rows: tuple[str, str, str]) -> bytes:
    """A `corpCode.xml` archive holding the rows given."""
    body = "".join(
        f"<list><corp_code>{code}</corp_code><corp_name>{name}</corp_name>"
        f"<stock_code>{stock}</stock_code></list>"
        for code, name, stock in rows
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("CORPCODE.xml", f"<result>{body}</result>")
    return buffer.getvalue()


def refusal(status: str, message: str = "오류") -> bytes:
    """What DART sends instead of an archive when it will not serve one."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f"<result><status>{status}</status><message>{message}</message></result>"
    ).encode()


class TestAnArchiveIsParsed:
    def test_listed_companies_come_back(self) -> None:
        found = KrxMasterCollector.parse_corp_codes(
            archive(("00126380", "삼성전자", "005930"), ("00164779", "SK하이닉스", "000660"))
        )

        assert [c.stock_code for c in found] == ["005930", "000660"]

    def test_an_unlisted_company_is_dropped(self) -> None:
        """A padded, blank `stock_code` is most of the file."""
        found = KrxMasterCollector.parse_corp_codes(
            archive(("00126380", "삼성전자", "005930"), ("00999999", "비상장회사", "  "))
        )

        assert [c.name for c in found] == ["삼성전자"]


def no_xml_member() -> bytes:
    """A valid ZIP whose members are all something else."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("readme.txt", "not the file we wanted")
    return buffer.getvalue()


def corrupt_member() -> bytes:
    """A ZIP whose compressed bytes are damaged in transit.

    The likeliest of these in practice: a proxy or CDN returns a body that is
    complete by length and wrong by content, so the transport sees nothing
    amiss and `zlib` raises on decompression.
    """
    good = bytearray(archive(("00126380", "삼성전자", "005930")))
    good[60:90] = bytes(30)
    return bytes(good)


def member_not_utf8() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("CORPCODE.xml", bytes([0xFF, 0xFE, 0x00]) + b"garbage")
    return buffer.getvalue()


def member_not_xml() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("CORPCODE.xml", "502 Bad Gateway, from somewhere in the middle")
    return buffer.getvalue()


class TestAnArchiveThatBeginsPKIsStillNotTrusted:
    """Checking four bytes moves the boundary; it does not close it.

    Everything below the ZIP header is still the outside world. A body with no
    XML member, a damaged member, bytes that are not UTF-8 or text that is not
    XML each raise something that is not a `CollectorError`, so `run_collector`
    re-raises it and records the run as an internal defect.
    """

    def test_an_archive_without_an_xml_member_is_an_outage(self) -> None:
        with pytest.raises(UpstreamUnavailableError, match="no XML member"):
            KrxMasterCollector.parse_corp_codes(no_xml_member())

    def test_a_damaged_member_is_an_outage(self) -> None:
        with pytest.raises(UpstreamUnavailableError, match="damaged"):
            KrxMasterCollector.parse_corp_codes(corrupt_member())

    def test_a_truncated_archive_is_an_outage(self) -> None:
        with pytest.raises(UpstreamUnavailableError):
            KrxMasterCollector.parse_corp_codes(archive(("00126380", "삼성전자", "005930"))[:200])

    def test_a_member_that_is_not_utf8_is_an_outage(self) -> None:
        with pytest.raises(UpstreamUnavailableError, match="not UTF-8"):
            KrxMasterCollector.parse_corp_codes(member_not_utf8())

    def test_a_member_that_is_not_xml_is_an_outage(self) -> None:
        with pytest.raises(UpstreamUnavailableError, match="not XML"):
            KrxMasterCollector.parse_corp_codes(member_not_xml())


class TestARefusalIsNotAnArchive:
    def test_a_quota_refusal_is_a_rate_limit(self) -> None:
        """Their counter and ours disagree, which is an accounting bug here."""
        with pytest.raises(RateLimitedError, match="ledger"):
            KrxMasterCollector.parse_corp_codes(refusal("020", "요청 제한을 초과하였습니다"))

    @pytest.mark.parametrize("status", ["010", "011", "012", "013", "101", "800", "900", "901"])
    def test_every_other_status_is_an_upstream_failure(self, status: str) -> None:
        with pytest.raises(UpstreamUnavailableError) as raised:
            KrxMasterCollector.parse_corp_codes(refusal(status))

        assert status in str(raised.value)

    def test_the_failure_says_what_the_code_means(self) -> None:
        """A bare number in a run record costs whoever reads it a search."""
        with pytest.raises(UpstreamUnavailableError, match="등록되지 않은 키"):
            KrxMasterCollector.parse_corp_codes(refusal("010"))

    def test_an_undocumented_status_is_still_classified(self) -> None:
        with pytest.raises(UpstreamUnavailableError, match="undocumented"):
            KrxMasterCollector.parse_corp_codes(refusal("077"))

    def test_a_body_that_is_neither_is_classified_too(self) -> None:
        """An HTML error page from a proxy, say. Still the outside world."""
        with pytest.raises(UpstreamUnavailableError, match="neither an archive"):
            KrxMasterCollector.parse_corp_codes(b"<html><body>502 Bad Gateway</body></html>")

    def test_an_empty_body_does_not_reach_zipfile(self) -> None:
        with pytest.raises(UpstreamUnavailableError):
            KrxMasterCollector.parse_corp_codes(b"")

    @pytest.mark.parametrize(
        "payload",
        [
            refusal("020"),
            refusal("010"),
            b"",
            b"garbage",
            # Bodies that begin `PK` and are still not a usable archive. The
            # magic-byte check waves every one of these through, and each used
            # to escape as something other than a CollectorError.
            no_xml_member(),
            corrupt_member(),
            member_not_utf8(),
            member_not_xml(),
            archive(("00126380", "삼성전자", "005930"))[:200],
        ],
        # Named explicitly. A ZIP carries the moment it was written, so the
        # generated ids differ between xdist workers and collection disagrees.
        ids=[
            "status-020",
            "status-010",
            "empty",
            "garbage",
            "zip-without-xml-member",
            "zip-damaged-member",
            "zip-member-not-utf8",
            "zip-member-not-xml",
            "zip-truncated",
        ],
    )
    def test_nothing_escapes_as_a_bare_zip_error(self, payload: bytes) -> None:
        """The regression itself.

        `BadZipFile` is not a `CollectorError`, so `run_collector` re-raises it
        and files the run as an internal defect. Every refusal has to arrive as
        a typed collector failure instead.
        """
        with pytest.raises(CollectorError):
            KrxMasterCollector.parse_corp_codes(payload)

        assert not issubclass(zipfile.BadZipFile, CollectorError)


class TestQuartersCoverTheYearDartAllows:
    def test_no_range_exceeds_the_documented_cap(self) -> None:
        """DART refuses wider than three months without a `corp_code`."""
        ranges = list(KrxMasterCollector.quarters(end=date(2026, 9, 22), years_back=1))

        assert ranges
        for start, finish in ranges:
            assert (finish - start).days < 90

    def test_the_ranges_neither_overlap_nor_leave_gaps(self) -> None:
        """Newest first, so each range ends the day before the previous begins."""
        ranges = list(KrxMasterCollector.quarters(end=date(2026, 9, 22), years_back=1))

        assert len(ranges) > 1
        for (newer_start, _), (_, older_end) in pairwise(ranges):
            assert older_end == newer_start - timedelta(days=1)

    def test_the_newest_quarter_comes_first(self) -> None:
        """A budget running out should cost the oldest quarter, not the newest."""
        ranges = list(KrxMasterCollector.quarters(end=date(2026, 9, 22), years_back=2))

        assert ranges == sorted(ranges, key=lambda r: r[0], reverse=True)
