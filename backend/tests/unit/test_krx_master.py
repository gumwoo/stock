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

import httpx
import pytest

from app.collectors.base import CollectorError, RateLimitedError, UpstreamUnavailableError, as_text
from app.collectors.krx_master import MAX_PAGES_PER_QUARTER, KrxMasterCollector


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
        found, _ = KrxMasterCollector.parse_corp_codes(
            archive(("00126380", "삼성전자", "005930"), ("00164779", "SK하이닉스", "000660"))
        )

        assert [c.stock_code for c in found] == ["005930", "000660"]

    def test_an_unlisted_company_is_dropped(self) -> None:
        """A padded, blank `stock_code` is most of the file."""
        found, _ = KrxMasterCollector.parse_corp_codes(
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


def encrypted_member() -> bytes:
    """A ZIP whose member needs a password. `zipfile` raises `RuntimeError`."""
    good = bytearray(archive(("00126380", "삼성전자", "005930")))
    # Set the encrypted bit in the local header and the central directory.
    good[6] |= 0x01
    index = good.rfind(b"PK\x01\x02")
    good[index + 8] |= 0x01
    return bytes(good)


def unsupported_compression() -> bytes:
    """Compression method 99, which WinZip uses for AES.

    `zipfile` raises `NotImplementedError` — not a `CollectorError`, and not an
    external failure either, so it used to be filed as a defect in our code.
    """
    good = bytearray(archive(("00126380", "삼성전자", "005930")))
    good[8:10] = (99).to_bytes(2, "little")
    index = good.rfind(b"PK\x01\x02")
    good[index + 10 : index + 12] = (99).to_bytes(2, "little")
    return bytes(good)


def corrupt_lzma_member() -> bytes:
    """An lzma member whose compressed payload is damaged: `LZMAError`.

    The damage has to land inside the member's own data. Corrupting the
    central directory instead just gives `BadZipFile`, which is a different
    failure and was already covered.
    """
    body = "<result>" + "<list><corp_code>0012</corp_code></list>" * 400 + "</result>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_LZMA) as zf:
        zf.writestr("CORPCODE.xml", body)
    raw = bytearray(buffer.getvalue())

    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as archive_in:
        info = archive_in.getinfo("CORPCODE.xml")
    data_at = info.header_offset + 30 + len(info.filename) + len(info.extra)
    # Past the 9-byte LZMA properties header, well inside the stream.
    raw[data_at + 30 : data_at + 60] = bytes(30)
    return bytes(raw)


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
        with pytest.raises(UpstreamUnavailableError) as raised:
            KrxMasterCollector.parse_corp_codes(no_xml_member())

        message = str(raised.value)
        assert "no XML member" in message
        # Not wrapped a second time by the broad catch around it. Without the
        # `except CollectorError: raise` ahead of that catch, this arrives as
        # "archive is unreadable: UpstreamUnavailableError: ...", which still
        # contains the words above and says the wrong thing about the cause.
        assert "unreadable" not in message
        assert raised.value.__cause__ is None

    def test_a_damaged_member_is_an_outage(self) -> None:
        with pytest.raises(UpstreamUnavailableError, match="unreadable"):
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
            # The three the broad catch was written for. Listing exception
            # types instead of catching broadly left all three escaping, and
            # the commit that claimed to fix them had no test that touched one.
            encrypted_member(),
            unsupported_compression(),
            corrupt_lzma_member(),
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
            "zip-encrypted-member",
            "zip-unsupported-compression",
            "zip-corrupt-lzma-member",
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


class NoWait:
    """The rate limiter, minus the waiting."""

    def acquire(self) -> None:
        return None


class CountingGuard:
    def __init__(self) -> None:
        self.reserved = 0

    def reserve(self, group: str, endpoint: str, *, calls: int = 1, now: object = None) -> None:
        self.reserved += 1


def answering(handler: object) -> tuple[KrxMasterCollector, httpx.Client]:
    c = KrxMasterCollector(guard=CountingGuard(), fill_gaps=False)  # type: ignore[arg-type]
    c._key = "test-key"
    c._bucket = NoWait()  # type: ignore[assignment]
    return c, httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


class TestTheJsonEndpointsHaveShapesToo:
    """The same discipline the archive got, for the endpoints beside it.

    A payload that is not the shape the code assumes raises `TypeError`,
    `ValueError` or `AttributeError` — none of them a `CollectorError` — so
    `run_collector` re-raises and files an outage as a defect in our code.
    """

    def test_a_payload_that_is_not_an_object_is_an_outage(self) -> None:
        c, client = answering(lambda _r: httpx.Response(200, json=[1, 2, 3]))
        with client, pytest.raises(UpstreamUnavailableError, match="not an object"):
            c._get_json(client, "list.json")

    def test_rows_that_are_not_a_list_are_an_outage(self) -> None:
        """`{"list": 7}` used to reach `for row in 7`."""
        c, client = answering(
            lambda _r: httpx.Response(200, json={"status": "000", "total_page": 1, "list": 7})
        )
        with client, pytest.raises(UpstreamUnavailableError, match="where rows were expected"):
            c.boards_from_filings(client, end=date(2026, 9, 22))

    def test_a_row_that_is_not_an_object_is_skipped(self) -> None:
        """One bad row costs that row, not the sweep."""
        c, client = answering(
            lambda _r: httpx.Response(
                200,
                json={
                    "status": "000",
                    "total_page": 1,
                    "list": ["nonsense", {"corp_code": "00126380", "corp_cls": "Y"}],
                },
            )
        )
        with client:
            boards, stopped = c.boards_from_filings(client, end=date(2026, 9, 22))

        assert stopped is None
        assert boards["00126380"] == "Y"

    def test_a_page_count_that_is_not_a_number_is_an_outage(self) -> None:
        c, client = answering(
            lambda _r: httpx.Response(200, json={"status": "000", "total_page": "many", "list": []})
        )
        with client, pytest.raises(UpstreamUnavailableError, match="total_page"):
            c.boards_from_filings(client, end=date(2026, 9, 22))

    def test_an_absurd_page_count_cannot_spend_the_day(self) -> None:
        """A wrong number must not turn the loop into the day's whole budget.

        Without a cap this pages until the quota guard refuses, which is one
        malformed field costing every other DART collector its day.

        The transport refuses past the ceiling rather than answering forever.
        A test that proves a loop is bounded by running the unbounded version
        does not fail, it hangs — and a hung suite is worse than a red one.
        """
        quarters = len(list(KrxMasterCollector.quarters(end=date(2026, 9, 22), years_back=1)))
        ceiling = quarters * MAX_PAGES_PER_QUARTER
        served = 0

        def handler(_r: httpx.Request) -> httpx.Response:
            nonlocal served
            served += 1
            if served > ceiling + 10:
                raise AssertionError(f"paged past the cap: {served} requests")
            return httpx.Response(200, json={"status": "000", "total_page": 10**9, "list": []})

        c, client = answering(handler)
        with client:
            c.boards_from_filings(client, end=date(2026, 9, 22))

        assert served <= ceiling
        assert c._guard.reserved == served  # type: ignore[attr-defined]


class TestAFieldWiderThanItsColumn:
    """`corp_name` is `String(200)`. DART is not obliged to agree.

    A value the database refuses arrives as a `DataError` from a flush partway
    through a sweep of several thousand rows — and it takes the run record with
    it, because the handler that files the failure commits on the same session
    the failed flush deactivated.
    """

    def test_an_overlong_name_drops_that_candidate_only(self) -> None:
        found, _ = KrxMasterCollector.parse_corp_codes(
            archive(
                ("00126380", "삼성전자", "005930"),
                ("00164779", "가" * 900, "000660"),
            )
        )

        assert [c.name for c in found] == ["삼성전자"]

    def test_an_overlong_corp_code_drops_that_candidate(self) -> None:
        found, _ = KrxMasterCollector.parse_corp_codes(
            archive(("0" * 40, "긴코드회사", "005931"), ("00126380", "삼성전자", "005930"))
        )

        assert [c.corp_code for c in found] == ["00126380"]

    def test_an_overlong_stock_code_drops_that_candidate(self) -> None:
        found, _ = KrxMasterCollector.parse_corp_codes(
            archive(("00126380", "삼성전자", "005930"), ("00164779", "긴종목", "9" * 40))
        )

        assert [c.stock_code for c in found] == ["005930"]


class TestTheDropIsReported:
    """A company that leaves the master without a word is a silent loss."""

    def test_the_count_comes_back_with_the_candidates(self) -> None:
        found, dropped = KrxMasterCollector.parse_corp_codes(
            archive(
                ("00126380", "삼성전자", "005930"),
                ("00164779", "가" * 900, "000660"),
                ("0" * 40, "긴코드", "000661"),
            )
        )

        assert [c.name for c in found] == ["삼성전자"]
        assert dropped == 2

    def test_a_clean_archive_drops_nothing(self) -> None:
        _, dropped = KrxMasterCollector.parse_corp_codes(
            archive(("00126380", "삼성전자", "005930"))
        )

        assert dropped == 0


class TestTheArchiveFailuresTheCatchIsFor:
    """Named one by one, because listing exception types missed all three.

    `zipfile` raises `RuntimeError` for an encrypted member,
    `NotImplementedError` for a compression method it does not implement, and
    `LZMAError` for a damaged lzma payload. None of them is a `CollectorError`
    or an `OSError`, so each was recorded as a defect in this file and the
    sweep died. The broad catch is what covers them; these say so.
    """

    def test_an_encrypted_member_is_an_outage(self) -> None:
        with pytest.raises(UpstreamUnavailableError, match="unreadable"):
            KrxMasterCollector.parse_corp_codes(encrypted_member())

    def test_an_unsupported_compression_method_is_an_outage(self) -> None:
        with pytest.raises(UpstreamUnavailableError, match="unreadable"):
            KrxMasterCollector.parse_corp_codes(unsupported_compression())

    def test_a_damaged_lzma_member_is_an_outage(self) -> None:
        with pytest.raises(UpstreamUnavailableError, match="unreadable"):
            KrxMasterCollector.parse_corp_codes(corrupt_lzma_member())


class TestAFieldIsNotAStringBecauseTheRowIsADict:
    """The same guard, one layer down.

    `isinstance(row, dict)` stops a scalar row and then `row["corp_code"]`
    raises anyway when the field itself arrived as a JSON number.
    """

    def test_a_numeric_row_field_is_an_absent_field(self) -> None:
        c, client = answering(
            lambda _r: httpx.Response(
                200,
                json={
                    "status": "000",
                    "total_page": 1,
                    "list": [
                        {"corp_code": 126380, "corp_cls": "Y"},
                        {"corp_code": "00164779", "corp_cls": "K"},
                    ],
                },
            )
        )
        with client:
            boards, stopped = c.boards_from_filings(client, end=date(2026, 9, 22))

        assert stopped is None
        assert boards == {"00164779": "K"}

    def test_a_numeric_board_is_an_absent_board(self) -> None:
        c, client = answering(
            lambda _r: httpx.Response(
                200,
                json={
                    "status": "000",
                    "total_page": 1,
                    "list": [{"corp_code": "00126380", "corp_cls": 1}],
                },
            )
        )
        with client:
            boards, _ = c.boards_from_filings(client, end=date(2026, 9, 22))

        assert boards == {}

    def test_a_number_is_never_coerced_into_a_code(self) -> None:
        """`00126380` as a JSON number is 126380, and the zeros are the code.

        The helper lives in `base` now: the same guard was reinvented in four
        collectors, one layer deeper each time.
        """
        assert as_text({"corp_code": 126380}, "corp_code") == ""
        assert as_text({"corp_code": "00126380"}, "corp_code") == "00126380"
        assert as_text({"corp_code": None}, "corp_code") == ""
        assert as_text({}, "corp_code") == ""


def raw_archive(rows: str) -> bytes:
    """An archive whose XML is written out literally, fields and all."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("CORPCODE.xml", f"<result>{rows}</result>")
    return buffer.getvalue()


class TestWhatCountsAsALoss:
    """An unlisted company is not a loss. A listed one that lost a field is.

    DART's file holds far more unlisted companies than listed ones, so counting
    those would drown the number that matters. But a row that was plainly
    listed and arrived missing its name is the silent loss this count exists
    for, and the first version of it looked only at over-long fields.
    """

    def test_an_unlisted_company_is_not_counted(self) -> None:
        _, dropped = KrxMasterCollector.parse_corp_codes(
            raw_archive(
                "<list><corp_code>00999999</corp_code><corp_name>비상장</corp_name>"
                "<stock_code> </stock_code></list>"
                "<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
                "<stock_code>005930</stock_code></list>"
            )
        )

        assert dropped == 0

    def test_a_listed_company_missing_its_name_is_counted(self) -> None:
        found, dropped = KrxMasterCollector.parse_corp_codes(
            raw_archive(
                "<list><corp_code>00999998</corp_code><corp_name> </corp_name>"
                "<stock_code>009999</stock_code></list>"
                "<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
                "<stock_code>005930</stock_code></list>"
            )
        )

        assert [c.name for c in found] == ["삼성전자"]
        assert dropped == 1

    def test_a_listed_company_missing_its_corp_code_is_counted(self) -> None:
        _, dropped = KrxMasterCollector.parse_corp_codes(
            raw_archive(
                "<list><corp_code></corp_code><corp_name>코드없음</corp_name>"
                "<stock_code>009998</stock_code></list>"
            )
        )

        assert dropped == 1

    def test_a_file_of_unlisted_companies_reports_none_dropped(self) -> None:
        """The real file is mostly these. A count that included them says
        nothing, which is the same as not having it."""
        rows = "".join(
            f"<list><corp_code>0099{i:04d}</corp_code><corp_name>비상장{i}</corp_name>"
            "<stock_code></stock_code></list>"
            for i in range(50)
        )
        found, dropped = KrxMasterCollector.parse_corp_codes(raw_archive(rows))

        assert found == []
        assert dropped == 0
