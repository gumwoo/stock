"""전일 상한가: 상한가 가격(호가단위 두 번 내림과 같은 값), 상태 판정, 직전 세션 봉만 쓰기, 표시 실패 격리, 사후 묶음."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.scoring.price_limit import CLOSED, LOCKED, TOUCHED, state, tick, upper_limit
from app.services import list_review_service, price_limit_service
from app.services.list_review_service import Row


def dec(x: float | str) -> Decimal:
    return Decimal(str(x))


@pytest.mark.parametrize(
    ("price", "size"),
    [
        (1_999, 1),
        (2_000, 5),
        (4_995, 5),
        (5_000, 10),
        (49_950, 50),
        (50_000, 100),
        (199_900, 100),
        (200_000, 500),
        (499_500, 500),
        (500_000, 1_000),
    ],
)
def test_tick_sizes(price: int, size: int) -> None:
    assert tick(price) == size


@pytest.mark.parametrize(
    ("base", "limit"),
    [
        (30_550, 39_700),  # HLB 9/23 → 9/28
        (7_400, 9_620),  # HLB제약
        (657, 854),  # 베노티앤알
        (4_020, 5_220),  # 결과 가격 구간(10원)으로 한 번 더 내림
        (38_500, 50_000),  # 50,050 → 100원 구간
    ],
)
def test_upper_limit(base: int, limit: int) -> None:
    assert upper_limit(dec(base)) == limit


@pytest.mark.parametrize("base", ["5346.39", "4021", "0"])
def test_an_adjusted_or_off_tick_base_is_not_judged(base: str) -> None:
    # 소수점(조정된 봉), 호가단위에 안 맞는 값(4,021은 5원 구간)
    assert upper_limit(dec(base)) is None


class TestState:
    def test_locked_all_day(self) -> None:
        assert state(dec(39700), dec(39700), dec(39700), dec(39700), dec(30550)) == LOCKED

    def test_closed_at_the_limit_after_trading_below(self) -> None:
        assert state(dec(1528), dec(1528), dec(1421), dec(1528), dec(1176)) == CLOSED

    def test_touched_and_fell_back(self) -> None:
        assert state(dec(657), dec(854), dec(641), dec(769), dec(657)) == TOUCHED

    def test_an_ordinary_day(self) -> None:
        assert state(dec(100), dec(110), dec(95), dec(105), dec(100)) is None

    def test_a_bar_stopped_before_the_second_rounding_still_counts(self) -> None:
        # 기준 4,020: 규정상 5,220인데 봉이 5,225로 남아 있는 경우
        assert state(dec(5225), dec(5225), dec(5225), dec(5225), dec(4020)) == LOCKED


def _bar(day: date, o: float, h: float, l: float, c: float) -> Any:  # noqa: E741
    return SimpleNamespace(
        ts=datetime(day.year, day.month, day.day, 0, 0, tzinfo=UTC),
        open=dec(o),
        high=dec(h),
        low=dec(l),
        close=dec(c),
    )


class TestPrevLimits:
    def test_the_previous_session_after_the_chuseok_break(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def history(_s: object, _i: int, _iv: object, **kw: Any) -> list[Any]:
            seen.update(kw)
            return [
                _bar(date(2026, 9, 23), 31300, 31550, 30050, 30550),
                _bar(date(2026, 9, 28), 39700, 39700, 39700, 39700),
            ]

        monkeypatch.setattr(price_limit_service.candle_repo, "history", history)
        asof = datetime(2026, 9, 28, 23, 50, tzinfo=UTC)
        out = price_limit_service.prev_limits(None, date(2026, 9, 29), [1], ingested_before=asof)  # type: ignore[arg-type]
        assert out[1].state == LOCKED and out[1].day == date(2026, 9, 28)
        assert out[1].change_pct == pytest.approx(29.95)
        # 9/28 마감 전에 끝난 봉만, 목록을 얼린 시각까지 들어온 수정본만
        assert seen["available_before"] == datetime(2026, 9, 28, 6, 30, tzinfo=UTC)
        assert seen["ingested_before"] == asof

    def test_a_missing_previous_bar_is_not_judged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            price_limit_service.candle_repo,
            "history",
            lambda *_a, **_k: [
                _bar(date(2026, 9, 22), 1, 1, 1, 1),
                _bar(date(2026, 9, 23), 1, 1, 1, 1),
            ],
        )
        assert price_limit_service.prev_limits(None, date(2026, 9, 29), [1]) == {}  # type: ignore[arg-type]


def test_review_groups_the_prior_day_limit_names() -> None:
    def row(name: str, limit: str | None, oc: float) -> Row:
        return Row(
            name, "WATCH", ("DISCLOSURE_EVENT",), False, None, 0.0, oc, 0.0, 1.0, limit, None
        )

    g = {
        s.label: s
        for s in list_review_service.groups(
            [
                row("a", LOCKED, -1.0),
                row("b", LOCKED, 1.0),
                row("c", CLOSED, -16.0),
                row("d", None, 2.0),
            ]
        )
    }
    assert g["전일 점상한가"].n == 2 and g["전일 점상한가"].open_close == pytest.approx(0.0)
    assert g["전일 상한가 마감(장중 거래)"].n == 1
    assert "전일 상한가 터치" not in g


def test_a_failure_to_read_limits_leaves_the_screens_as_they_were(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    from app.api import lists
    from app.realtime import gateway

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(price_limit_service, "prev_limits", boom)
    session: Any = SimpleNamespace(begin_nested=nullcontext)
    assert lists._limits(session, date(2026, 9, 29), [1], None) == {}
    assert lists.limit_fields(None) == {"prev_limit": None, "prev_change_pct": None}
    member = gateway.LiveMember(
        instrument_id=1,
        code="047920",
        name="HLB제약",
        rank=1,
        reasons=(),
        overlay_points=None,
        attention_surge=None,
        regime=None,
    )
    created = datetime(2026, 9, 28, 23, 50, tzinfo=UTC)
    assert gateway.attach_limits([member], date(2026, 9, 29), created) == [member]
    assert gateway.member_dict(member)["prev_limit"] is None


def test_the_batch_lookup_reads_the_same_bars_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    # 전략 실험실은 목록 날마다 한 번에 읽는다(`prev_limits_many`). 같은 봉이면 `prev_limits`와 같은 답이어야 한다.
    by_id = {
        1: [
            _bar(date(2026, 9, 23), 31300, 31550, 30050, 30550),
            _bar(date(2026, 9, 28), 39700, 39700, 39700, 39700),
        ],
        2: [
            _bar(date(2026, 9, 22), 1, 1, 1, 1),
            _bar(date(2026, 9, 23), 1, 1, 1, 1),
        ],  # 직전 세션 봉 없음
        3: [_bar(date(2026, 9, 28), 100, 110, 95, 105)],  # 기준 봉 없음
    }
    seen: dict[str, Any] = {}

    def many(_s: object, ids: list[int], _iv: object, **kw: Any) -> dict[int, list[Any]]:
        seen.update(kw)
        return {i: by_id[i] for i in ids}

    monkeypatch.setattr(
        price_limit_service.candle_repo, "history", lambda _s, i, _iv, **_k: by_id[i]
    )
    monkeypatch.setattr(price_limit_service.candle_repo, "history_many", many)
    asof = datetime(2026, 9, 28, 23, 50, tzinfo=UTC)
    one = price_limit_service.prev_limits(None, date(2026, 9, 29), [1, 2, 3], ingested_before=asof)  # type: ignore[arg-type]
    batch = price_limit_service.prev_limits_many(
        None, date(2026, 9, 29), [1, 2, 3], ingested_before=asof
    )  # type: ignore[arg-type]
    assert batch == one and set(batch) == {1}
    assert seen["available_before"] == datetime(2026, 9, 28, 6, 30, tzinfo=UTC)
    assert seen["ingested_before"] == asof
