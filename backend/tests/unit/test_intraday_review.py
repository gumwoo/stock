"""The morning lists' questions: paired by day, fixed sample, and when each is settled."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.scoring.intraday_review import DECIDE_DAYS, READ_DAYS, MemberDay, evaluate


def member(n: int, rank: int, reasons: tuple[str, ...], ret: float, **kw: object) -> MemberDay:
    return MemberDay(
        day=date(2026, 10, 1) + timedelta(days=n),
        rank=rank,
        reasons=reasons,
        regime="RISK_ON",
        return_pct=ret,
        first_hour_pct=kw.get("first_hour"),  # type: ignore[arg-type]
        market_return_pct=kw.get("market"),  # type: ignore[arg-type]
    )


def days_of(n: int, good: float, other: float, *, start: int = 0) -> list[MemberDay]:
    out = []
    for d in range(start, start + n):
        wobble = 0.2 if d % 2 else -0.2
        # The difference itself moves a little from day to day, as a real one would.
        spread = 0.1 * (d % 3 - 1)
        out.append(member(d, 1, ("POSITIVE_NEWS_OVERLAY",), good + wobble + spread))
        out.append(member(d, 15, ("TRACKED",), other + wobble - spread))
    return out


def result(members: list[MemberDay], key: str) -> object:
    return next(r for r in evaluate(members) if r.key == key)


class TestPairedByDay:
    def test_each_day_is_one_difference_against_that_days_list(self) -> None:
        r = result(days_of(3, good=2.0, other=0.0), "H1")
        # Good news 2 against a list averaging 1: one point a day.
        assert r.days == 3 and r.mean == pytest.approx(1.0, abs=0.1)

    def test_a_day_without_the_group_says_nothing(self) -> None:
        members = [*days_of(2, good=2.0, other=0.0), member(9, 3, ("TRACKED",), -5.0)]
        assert result(members, "H1").days == 2

    def test_the_list_against_its_market_is_name_by_name_excess(self) -> None:
        members = [
            member(0, 1, ("TRACKED",), 3.0, market=1.0),
            member(0, 2, ("TRACKED",), 1.0, market=1.0),
        ]
        assert result(members, "H5").mean == pytest.approx(1.0)

    def test_ranks_one_to_ten_against_the_rest(self) -> None:
        members = [member(0, 3, ("TRACKED",), 2.0), member(0, 11, ("TRACKED",), -1.0)]
        assert result(members, "H4").mean == pytest.approx(3.0)

    def test_the_first_hour_for_search_surges(self) -> None:
        members = [
            member(0, 1, ("SEARCH_SURGE",), 0.0, first_hour=2.5),
            member(0, 2, ("TRACKED",), 0.0, first_hour=0.5),
        ]
        assert result(members, "H3").mean == pytest.approx(2.0)


class TestGates:
    def test_under_twenty_days_is_not_enough(self) -> None:
        assert result(days_of(READ_DAYS - 1, 2.0, 0.0), "H1").state == "not enough days"

    def test_twenty_to_sixty_is_read_not_decided(self) -> None:
        assert result(days_of(READ_DAYS, 2.0, 0.0), "H1").state == "reading"

    def test_a_steady_difference_over_sixty_days_is_established(self) -> None:
        assert result(days_of(DECIDE_DAYS, 2.0, 0.0), "H1").state == "established"

    def test_decided_on_the_first_sixty_days_only(self) -> None:
        # Sixty good days, then forty that reverse: the decision stands on the first sixty.
        members = days_of(DECIDE_DAYS, 2.0, 0.0) + days_of(40, -8.0, 0.0, start=DECIDE_DAYS)
        r = result(members, "H1")
        assert (r.days, r.state) == (DECIDE_DAYS, "established")

    def test_a_reversal_within_the_sixty_is_not_established(self) -> None:
        members = days_of(30, 2.0, 0.0) + days_of(30, -1.0, 0.0, start=30)
        assert result(members, "H1").state == "not established"
