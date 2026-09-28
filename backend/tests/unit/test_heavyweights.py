"""지수 대형주 표시와 밤사이 미국 반도체: 순위표 고르기(이전 표만, 오래되면 없음, 사후 분석만 뒤 표),
밤사이 등락 정렬(한국 연휴 뒤 누적, 새 미국 세션 없으면 없음, 주식 분할 의심), 사후 통계 묶음, 표시 실패 격리."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.instrument import Listing
from app.services import heavyweight_service, list_review_service, overnight_service
from app.services.list_review_service import Row


def _utc(y: int, m: int, d: int, h: int = 20) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


# 2026-09-28 한국 개장 전 실제 ^SOX 종가(미국 동부 16:00 = 20:00 UTC). 한국은 9/24~9/25 추석 휴장.
SOX = [
    (_utc(2026, 9, 22), 12689.820312),
    (_utc(2026, 9, 23), 12534.280273),
    (_utc(2026, 9, 24), 12492.540039),
    (_utc(2026, 9, 25), 12668.929688),
]
KR_OPEN_0928 = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)  # 09:00 KST
KR_CLOSE_0923 = datetime(2026, 9, 23, 6, 30, tzinfo=UTC)  # 15:30 KST


class TestOvernight:
    def test_after_a_korean_holiday_the_us_sessions_add_up(self) -> None:
        r = overnight_service.reference("^SOX", SOX, KR_OPEN_0928, KR_CLOSE_0923)
        # 9/22 종가(한국 9/23 마감 전 마지막) 대비 9/25 종가
        assert r["change_pct"] == pytest.approx(-0.16)
        assert r["us_sessions"] == ["2026-09-23", "2026-09-24", "2026-09-25"]
        assert r["label"] == "필라델피아 반도체" and not r["split_suspect"]

    def test_no_new_us_session_means_no_value(self) -> None:
        # 한국 9/25 개장(가정) 전에 새 미국 종가가 없다면: 9/24 20:00 UTC 뒤 마감, 다음 날 00:00 개장 사이 없음
        r = overnight_service.reference(
            "^SOX", SOX[:3], datetime(2026, 9, 25, 0, 0, tzinfo=UTC), _utc(2026, 9, 24, 21)
        )
        assert r["change_pct"] is None and r["us_sessions"] == []

    def test_a_stock_split_jump_is_hidden(self) -> None:
        split = [(_utc(2026, 9, 22), 1000.0), (_utc(2026, 9, 25), 100.0)]
        r = overnight_service.reference("NVDA", split, KR_OPEN_0928, KR_CLOSE_0923)
        assert r["change_pct"] is None and r["split_suspect"]

    def test_the_index_is_never_treated_as_a_split(self) -> None:
        big = [(_utc(2026, 9, 22), 1000.0), (_utc(2026, 9, 25), 600.0)]
        r = overnight_service.reference("^SOX", big, KR_OPEN_0928, KR_CLOSE_0923)
        assert r["change_pct"] == pytest.approx(-40.0) and not r["split_suspect"]


class _Session:
    def __init__(self, sectors: dict[int, str | None]) -> None:
        self.sectors = sectors

    def execute(self, _stmt: object) -> Any:
        return SimpleNamespace(all=lambda: list(self.sectors.items()))


def _ranks(
    monkeypatch: pytest.MonkeyPatch, *, before: date | None, after: date | None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def latest_day(_s: object, **kw: Any) -> date | None:
        calls.append(kw)
        return before if "before" in kw else after

    monkeypatch.setattr(heavyweight_service.market_cap_repo, "latest_day", latest_day)
    monkeypatch.setattr(
        heavyweight_service.market_cap_repo,
        "weights_on",
        lambda _s, _d, _ids: {1: (25.69, Listing.KOSPI), 2: (2.34, Listing.KOSPI)},
    )
    return calls


class TestWeights:
    def test_the_screen_uses_the_ranking_before_the_list_day(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _ranks(monkeypatch, before=date(2026, 9, 28), after=None)
        w = heavyweight_service.weights_for(
            _Session({1: "Semiconductors", 2: None}),
            date(2026, 9, 29),
            [1, 2],  # type: ignore[arg-type]
        )
        assert calls == [{"before": date(2026, 9, 29)}]
        assert w[1].heavyweight and w[1].listing == "KOSPI" and w[1].sector == "Semiconductors"
        assert not w[2].heavyweight and not w[1].after_the_fact

    def test_no_earlier_ranking_means_no_label_on_screen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ranks(monkeypatch, before=None, after=date(2026, 9, 28))
        assert heavyweight_service.weights_for(_Session({}), date(2026, 9, 28), [1]) == {}  # type: ignore[arg-type]

    def test_an_old_ranking_is_not_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ranks(monkeypatch, before=date(2026, 9, 1), after=None)
        assert heavyweight_service.weights_for(_Session({}), date(2026, 9, 28), [1]) == {}  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("rank_day", "used"), [(date(2026, 9, 10), True), (date(2026, 9, 9), False)]
    )
    def test_the_age_limit_is_ten_sessions(
        self, monkeypatch: pytest.MonkeyPatch, rank_day: date, used: bool
    ) -> None:
        # 9/10 표는 9/28 기준 10거래일 전(9/11~9/23, 9/28. 추석 9/24~25 휴장), 9/9 표는 11거래일 전
        _ranks(monkeypatch, before=rank_day, after=None)
        w = heavyweight_service.weights_for(_Session({1: None}), date(2026, 9, 28), [1])  # type: ignore[arg-type]
        assert bool(w) is used

    def test_the_review_may_use_the_same_day_marked_after_the_fact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ranks(monkeypatch, before=None, after=date(2026, 9, 28))
        w = heavyweight_service.weights_for(
            _Session({1: None}),
            date(2026, 9, 28),
            [1],
            allow_after=True,  # type: ignore[arg-type]
        )
        assert w[1].after_the_fact and w[1].rank_day == date(2026, 9, 28)


def _row(
    name: str, action: str, reasons: tuple[str, ...], oc: float, *, heavy: bool = False
) -> Row:
    return Row(name, action, reasons, heavy, 25.0 if heavy else 0.5, oc / 2, oc, -1.0, 2.0)


def test_review_groups_keep_heavyweights_apart() -> None:
    rows = [
        _row("a", "WATCH", ("DISCLOSURE_EVENT",), 2.0),
        _row("b", "WATCH", ("DISCLOSURE_EVENT", "SEARCH_SURGE"), -1.0),
        _row("c", "CAUTION", ("SEARCH_SURGE",), 1.0),
        _row("삼성전자", "WATCH", ("DISCLOSURE_EVENT",), -5.0, heavy=True),
    ]
    g = {s.label: s for s in list_review_service.groups(rows)}
    assert g["목록(대형주 뺌)"].n == 3 and g["목록(대형주 뺌)"].open_close == pytest.approx(2 / 3)
    assert g["판단 WATCH"].n == 2 and g["판단 CAUTION"].up_close == 1
    assert g["이유 DISCLOSURE_EVENT"].n == 2  # 대형주는 이유 묶음에도 들어가지 않는다
    assert g["지수 대형주"].n == 1 and g["지수 대형주"].vs_market == pytest.approx(-4.0)


def test_a_failure_to_read_weights_leaves_the_live_list_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.realtime import gateway

    member = gateway.LiveMember(
        instrument_id=1,
        code="005930",
        name="삼성전자",
        rank=1,
        reasons=("DISCLOSURE_EVENT",),
        overlay_points=None,
        attention_surge=None,
        regime=None,
    )

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(gateway.heavyweight_service, "weights_for", boom)
    assert gateway.attach_weights([member], date(2026, 9, 29)) == [member]


def test_a_failure_to_read_weights_leaves_the_signal_tab_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    from app.api import lists

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(lists.heavyweight_service, "weights_for", boom)
    session: Any = SimpleNamespace(begin_nested=nullcontext)
    assert lists._weights(session, date(2026, 9, 29), [1]) == {}
    assert lists.weight_fields(None) == {
        "market_weight_pct": None,
        "market_listing": None,
        "heavyweight": False,
        "sector": None,
    }
