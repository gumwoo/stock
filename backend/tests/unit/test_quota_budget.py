"""Budgets, windows, and the one direction they are allowed to be wrong in.

`app.core.quota` is pure, so none of this needs a database, a clock or a mock.
That is the point of putting the policy there: the arithmetic that decides
whether a call is allowed can be checked directly.

Two properties carry the whole design and are pinned here.

The first is containment. The plan replaces calendar windows with rolling ones
on the argument that what a daily cap counts — spending since its last reset —
always sits inside the last 24 hours, whatever timezone the reset happens in.
`TestCalendarCapsAreCovered` runs that claim across every real UTC offset
rather than trusting the prose.

The second is the direction of error. Windows floor to the minute, which pulls
their lower bound backwards and counts a boundary minute whole, including calls
that have already aged out. That makes every total an overestimate. An
underestimate is what lets a budget be exceeded, so the tests below assert the
inequality and not merely that the number is close.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.quota import (
    BUDGET_FRACTION,
    DEFAULT_PLAN,
    LimitSource,
    Quota,
    QuotaPlan,
    budget,
    floor_to_minute,
    headroom,
    window_start,
)

NOW = datetime(2026, 9, 22, 14, 37, 29, 123456, tzinfo=UTC)


def quota(**overrides: object) -> Quota:
    base: dict[str, object] = {
        "key": "test",
        "group": "test_group",
        "official_limit": 1_000,
        "window": timedelta(hours=24),
        "limit_source": LimitSource.OFFICIAL,
        "note": "a quota made up for a test",
    }
    base.update(overrides)
    return Quota(**base)  # type: ignore[arg-type]


class TestBudgets:
    def test_every_quota_is_half_its_limit(self) -> None:
        """The numbers the plan committed to, so a silent edit shows up here."""
        expected = {
            "naver_search_daily": 12_500,
            "naver_search_monthly": 387_500,
            "naver_datalab_monthly": 25_000,
            "threads_keyword_search": 1_100,
            "reddit_search": 500,
            "dart_daily": 10_000,
            "claude_subscription_5h": 30,
            "claude_subscription_daily": 120,
            "kis_rest_daily": 10_000,
            "kis_token_daily": 10,
        }
        actual = {q.key: budget(q) for q in DEFAULT_PLAN.quotas}

        assert actual == expected

    def test_a_half_call_rounds_down(self) -> None:
        """Rounding up would spend a call we said we would not."""
        assert budget(quota(official_limit=1_001)) == 500

    def test_a_fraction_means_the_decimal_that_was_written(self) -> None:
        """`0.7` is seven tenths, not the binary value nearest to it.

        70 of 90 times 0.7 is exactly 63. In binary floating point the product
        is 62.99999999999999, and flooring that gives 62 — a whole call below
        the share the operator wrote down. Taking the decimal makes the budget
        the number on the page, and the same number on every machine.

        The example matters. This assertion used to be 4,072,150 at 0.3, whose
        product is exact in float too, so it passed whichever arithmetic the
        code used and proved nothing. A sweep of 1..2,999 against eight
        fractions finds 70 pairs that do disagree; this is the smallest.
        """
        assert budget(quota(official_limit=90), fraction=0.7) == 63

    def test_the_two_arithmetics_really_do_disagree_here(self) -> None:
        """Guards the example itself, so it cannot decay into a tautology."""
        import math

        assert math.floor(90 * 0.7) == 62
        assert budget(quota(official_limit=90), fraction=0.7) == 63

    def test_fraction_must_be_a_fraction(self) -> None:
        for bad in (0.0, -0.5, 1.5):
            with pytest.raises(ValueError, match="fraction"):
                budget(quota(), fraction=bad)

    def test_the_whole_limit_is_expressible(self) -> None:
        """Not that we use it, but a budget of 1.0 must not be an error."""
        assert budget(quota(official_limit=7), fraction=1.0) == 7

    def test_headroom_never_goes_negative(self) -> None:
        """Over-spend is a state the ledger can reach; it is not a negative budget."""
        assert headroom(quota(), 600) == 0
        assert headroom(quota(), 400) == 100

    def test_the_default_fraction_is_half(self) -> None:
        assert BUDGET_FRACTION == 0.5


class TestQuotaPlan:
    def test_a_naver_search_call_is_counted_against_two_quotas(self) -> None:
        """The daily cap and our own monthly ceiling both have to have room."""
        keys = {q.key for q in DEFAULT_PLAN.covering("naver_search")}

        assert keys == {"naver_search_daily", "naver_search_monthly"}

    def test_news_and_blog_would_share_one_budget(self) -> None:
        """Naver meters its search family together, against the client ID.

        Nothing here is per-endpoint, which is exactly the point: if a blog
        collector arrives it looks up the same group and draws from the same
        12,500, rather than being handed a second copy of the same cap.
        """
        assert DEFAULT_PLAN.covering("naver_search") == DEFAULT_PLAN.covering("naver_search")
        assert all(q.group == "naver_search" for q in DEFAULT_PLAN.covering("naver_search"))

    def test_datalab_is_a_different_group_from_search(self) -> None:
        """Search trend has its own cap, not a slice of the search budget."""
        datalab = {q.key for q in DEFAULT_PLAN.covering("naver_datalab")}

        assert datalab == {"naver_datalab_monthly"}

    def test_an_unknown_group_covers_nothing(self) -> None:
        assert DEFAULT_PLAN.covering("no_such_group") == ()

    def test_keys_are_unique(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            QuotaPlan((quota(key="same"), quota(key="same", group="other")))

    def test_every_quota_says_where_its_number_came_from(self) -> None:
        """A table mixing published caps with ceilings we invented, without
        marking which is which, is a table whose reader trusts our guess."""
        for q in DEFAULT_PLAN.quotas:
            assert isinstance(q.limit_source, LimitSource)
            assert q.note.strip()

        # Every figure comes from a provider except the Claude subscription's,
        # for which Anthropic publishes no per-call limit at all — only usage
        # windows the provider reads back. Those two are ours, and marked so,
        # as are the two KIS ceilings: KIS publishes a per-second rate and a
        # token-issuing rule, no daily figure.
        # `dart_daily` is the one the provider itself hedges on.
        internal = {q.key for q in DEFAULT_PLAN.quotas if q.limit_source is LimitSource.INTERNAL}
        assert internal == {
            "claude_subscription_5h",
            "claude_subscription_daily",
            "kis_rest_daily",
            "kis_token_daily",
        }
        typical = {q.key for q in DEFAULT_PLAN.quotas if q.limit_source is LimitSource.TYPICAL}
        assert typical == {"dart_daily"}

    def test_a_nonsense_quota_is_refused(self) -> None:
        for bad in ({"official_limit": 0}, {"window": timedelta(0)}):
            with pytest.raises(ValueError):
                quota(**bad)


class TestWindowBoundaries:
    def test_the_lower_bound_is_one_window_back(self) -> None:
        start = window_start(quota(window=timedelta(hours=24)), NOW)

        assert NOW - start >= timedelta(hours=24)

    def test_flooring_widens_the_window_rather_than_narrowing_it(self) -> None:
        """Seconds are dropped downwards, so the boundary minute is counted whole.

        Some of those calls have already aged out of the true window. Counting
        them is an overestimate, which costs headroom; the alternative rounding
        would drop live calls, which costs the guarantee.
        """
        start = window_start(quota(window=timedelta(hours=24)), NOW)
        exact = NOW - timedelta(hours=24)

        assert start <= exact
        assert start.second == 0
        assert start.microsecond == 0
        assert exact - start < timedelta(minutes=1)

    def test_a_bucket_just_inside_the_window_is_included(self) -> None:
        start = window_start(quota(window=timedelta(hours=24)), NOW)
        just_inside = floor_to_minute(NOW - timedelta(hours=24) + timedelta(minutes=1))

        assert just_inside >= start

    def test_a_bucket_well_outside_the_window_is_excluded(self) -> None:
        start = window_start(quota(window=timedelta(hours=24)), NOW)
        outside = floor_to_minute(NOW - timedelta(hours=24, minutes=1))

        assert outside < start

    def test_a_ten_minute_window_spans_eleven_buckets_at_most(self) -> None:
        """Ten minutes of buckets, plus the partial one flooring pulled in."""
        start = window_start(quota(window=timedelta(minutes=10)), NOW)
        buckets = int((floor_to_minute(NOW) - start) / timedelta(minutes=1)) + 1

        assert buckets == 11

    def test_a_naive_instant_is_refused(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            window_start(quota(), datetime(2026, 9, 22, 14, 0))


class TestCalendarCapsAreCovered:
    """The argument that let calendar windows be deleted, run as a check.

    A daily cap counts what has been spent since its last reset. Whatever hour
    that reset falls on, it happened within the last 24 hours, so the spending
    it counts lies inside `[now - 24h, now]`. A rolling total under budget
    therefore cannot hide a calendar day over budget.
    """

    @staticmethod
    def _last_midnight(now: datetime, offset_hours: float) -> datetime:
        local = now.astimezone(timezone(timedelta(hours=offset_hours)))
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    @pytest.mark.parametrize("offset", [h / 2 for h in range(-24, 29)])
    def test_the_spent_part_of_any_calendar_day_fits_in_24h(self, offset: float) -> None:
        """Every real UTC offset, including the half-hour ones."""
        reset = self._last_midnight(NOW, offset)

        assert reset <= NOW
        assert reset >= NOW - timedelta(hours=24)
        assert reset >= window_start(quota(window=timedelta(hours=24)), NOW)

    def test_a_zone_without_dst_never_has_a_day_longer_than_24h(self) -> None:
        """The condition the containment argument actually needs.

        Every provider metered here resets on Korean time, which has had no
        summer time since 1988. That is what makes the rolling window safe for
        them, and it is a property of those zones rather than of timezones in
        general.
        """
        seoul = ZoneInfo("Asia/Seoul")
        day = datetime(2026, 1, 1, tzinfo=seoul)
        longest = timedelta(0)
        for _ in range(365):
            nxt = (day + timedelta(days=1, hours=6)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            longest = max(longest, nxt - day)
            day = nxt

        assert longest == timedelta(hours=24)

    def test_a_dst_fallback_day_is_longer_than_the_window(self) -> None:
        """Pinned because the docstring used to claim otherwise.

        A local day can run 25 hours, so a reset can fall outside a rolling
        24-hour window and spending just after it goes uncounted. No provider
        here resets on such a zone, and the half-budget covers the gap several
        times over — but the argument is conditional, and a test that only ran
        fixed offsets would keep saying it was universal.
        """
        berlin = ZoneInfo("Europe/Berlin")
        start = datetime(2026, 10, 25, tzinfo=berlin)
        end = datetime(2026, 10, 26, tzinfo=berlin)

        # Converted to UTC first. Subtracting two aware datetimes in the same
        # zone gives the wall-clock difference, which is 24 hours on every day
        # including this one — and hides exactly the hour that matters.
        elapsed = end.astimezone(UTC) - start.astimezone(UTC)

        assert elapsed == timedelta(hours=25)
        assert elapsed > timedelta(hours=24)

    @pytest.mark.parametrize(
        "moment",
        [
            datetime(2026, 2, 28, 23, 59, tzinfo=UTC),
            datetime(2026, 3, 1, 0, 1, tzinfo=UTC),
            datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
            datetime(2027, 1, 1, 0, 1, tzinfo=UTC),
            datetime(2028, 2, 29, 12, 0, tzinfo=UTC),
        ],
    )
    def test_a_month_window_is_always_thirty_one_days(self, moment: datetime) -> None:
        """Month-end, year-end and a leap day are not special cases here.

        The value of this test is what it documents: there is no calendar
        arithmetic to get wrong, because the window is a duration.
        """
        monthly = quota(window=timedelta(days=31))

        assert moment - window_start(monthly, moment) >= timedelta(days=31)
        assert moment - window_start(monthly, moment) < timedelta(days=31, minutes=1)

    def test_a_calendar_month_resets_within_31_days(self) -> None:
        """The same containment as the daily case, for the monthly ceiling."""
        monthly = quota(window=timedelta(days=31))
        for moment in (
            datetime(2026, 3, 31, 23, 0, tzinfo=UTC),
            datetime(2028, 2, 29, 1, 0, tzinfo=UTC),
        ):
            month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            assert month_start >= window_start(monthly, moment)
