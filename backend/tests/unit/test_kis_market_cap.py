"""시가총액 순위: 응답 변환(억원·비중·교차 점검), 세션 날짜(장중에는 받지 않음), 지수 대형주 기준."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.collectors.base import UpstreamUnavailableError
from app.collectors.kis_market_cap import finished_session, to_rows
from app.core.calendar import Market, MarketCalendar
from app.models.instrument import Listing
from app.scoring.heavyweight import is_heavyweight

KR = MarketCalendar(Market.KR)
DAY = date(2026, 9, 28)

# 2026-09-28 장 마감 뒤 실제 응답의 첫 두 행(필드만 옮김).
SAMSUNG = {
    "data_rank": "1",
    "mksc_shrn_iscd": "005930",
    "hts_kor_isnm": "삼성전자",
    "stck_prpr": "270500",
    "lstn_stcn": "5846278608",
    "stck_avls": "15814184",
    "mrkt_whol_avls_rlim": "25.69",
}
HYNIX = {
    "data_rank": "2",
    "mksc_shrn_iscd": "000660",
    "hts_kor_isnm": "SK하이닉스",
    "stck_prpr": "1762000",
    "lstn_stcn": "730492365",
    "stck_avls": "12871275",
    "mrkt_whol_avls_rlim": "20.91",
}


class TestToRows:
    def test_real_rows_parse_and_pass_the_cross_check(self) -> None:
        rows, warnings = to_rows(
            [SAMSUNG, HYNIX], Listing.KOSPI, DAY, {"005930": 1, "000660": 3}.get
        )
        assert warnings == []  # 종가 곱하기 상장주식수 / 1억 = 시가총액(억원)
        s = rows[0]
        assert (s.rank, s.code, s.instrument_id, s.name) == (1, "005930", 1, "삼성전자")
        assert s.market_cap_eok == Decimal("15814184") and s.weight_pct == pytest.approx(25.69)
        assert (s.session_date, s.listing) == (DAY, Listing.KOSPI)

    def test_a_unit_mismatch_is_reported(self) -> None:
        wrong = {**SAMSUNG, "stck_avls": "1581418400000000"}  # 원 단위로 왔다면
        _, warnings = to_rows([wrong], Listing.KOSPI, DAY, lambda c: None)
        assert warnings and "005930" in warnings[0]

    def test_unknown_codes_keep_no_instrument(self) -> None:
        rows, _ = to_rows([SAMSUNG], Listing.KOSPI, DAY, lambda c: None)
        assert rows[0].instrument_id is None

    @pytest.mark.parametrize("field", ["stck_avls", "mrkt_whol_avls_rlim", "data_rank"])
    def test_a_broken_number_is_an_upstream_error(self, field: str) -> None:
        with pytest.raises(UpstreamUnavailableError):
            to_rows([{**SAMSUNG, field: "abc"}], Listing.KOSPI, DAY, lambda c: None)


class TestFinishedSession:
    def test_after_the_close_is_that_day(self) -> None:
        assert finished_session(KR, datetime(2026, 9, 28, 7, 10, tzinfo=UTC)) == DAY  # 16:10 KST

    def test_during_the_session_it_does_not_collect(self) -> None:
        assert finished_session(KR, datetime(2026, 9, 28, 2, 0, tzinfo=UTC)) is None  # 11:00 KST

    def test_a_weekday_early_morning_does_not_collect(self) -> None:
        # 화 00:30 KST: 오늘이 세션인데 아직 안 끝났다. 전날 값을 오늘 날짜로 적지 않는다.
        assert finished_session(KR, datetime(2026, 9, 28, 15, 30, tzinfo=UTC)) is None

    def test_a_weekend_is_the_last_session(self) -> None:
        # 토 2026-10-03 12:00 KST → 금 10/2
        assert finished_session(KR, datetime(2026, 10, 3, 3, 0, tzinfo=UTC)) == date(2026, 10, 2)

    def test_a_holiday_is_the_session_before_it(self) -> None:
        # 추석 연휴 9/25(금) 12:00 KST → 9/23(수)
        assert finished_session(KR, datetime(2026, 9, 25, 3, 0, tzinfo=UTC)) == date(2026, 9, 23)


def test_heavyweight_threshold() -> None:
    assert is_heavyweight(25.69) and is_heavyweight(5.0)
    assert not is_heavyweight(2.34) and not is_heavyweight(None)
