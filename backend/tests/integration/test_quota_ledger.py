"""The ledger against a real database, and the guarantee it exists for.

`test_quota_budget.py` checks the arithmetic. This checks the two things only
Postgres can answer: that a refusal writes nothing, and that a reservation
survives the collector's transaction failing.

The second is the one worth having. A run that dies on its third page rolls
back its session, and if the ledger shared that session the two pages that
really did go out would disappear from the accounting along with the data. The
rollback is right about the data and wrong about the calls, which is why
`QuotaGuard` commits in a transaction of its own — and why a test that only
ever used one session would pass without exercising anything.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.quota import QuotaExhausted, QuotaGuard
from app.config import get_settings
from app.core.quota import LimitSource, Quota, QuotaPlan
from app.models import Base
from app.models.quota import ApiCallBucket

pytestmark = pytest.mark.integration

GROUP = "test_ledger"
OTHER = "test_ledger_other"

# Small enough to exhaust in a few calls. The budget is half of these, so the
# daily allowance is 5 and the long-window allowance is 8.
DAILY = Quota(
    key="test_daily",
    group=GROUP,
    official_limit=10,
    window=timedelta(hours=24),
    limit_source=LimitSource.OFFICIAL,
    note="test",
)
MONTHLY = Quota(
    key="test_monthly",
    group=GROUP,
    official_limit=16,
    window=timedelta(days=31),
    limit_source=LimitSource.INTERNAL,
    note="test",
)
PLAN = QuotaPlan(
    (
        DAILY,
        MONTHLY,
        Quota(
            key="test_other",
            group=OTHER,
            official_limit=4,
            window=timedelta(hours=24),
            limit_source=LimitSource.OFFICIAL,
            note="test",
        ),
    )
)

NOW = datetime(2026, 9, 22, 14, 37, 29, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine() -> Iterator[object]:
    eng = create_engine(get_settings().database_url, future=True)
    try:
        with eng.connect():
            pass
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def guard(engine: object) -> Iterator[tuple[QuotaGuard, sessionmaker[Session]]]:
    """A guard whose reservations commit independently, as in production."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]

    @contextmanager
    def scope() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    yield QuotaGuard(plan=PLAN, scope=scope), factory

    with factory() as cleanup:
        cleanup.execute(
            text("DELETE FROM api_call_bucket WHERE quota_group IN (:a, :b)"),
            {"a": GROUP, "b": OTHER},
        )
        cleanup.commit()


def total(factory: sessionmaker[Session], group: str = GROUP) -> int:
    with factory() as session:
        return int(
            session.execute(
                select(func.coalesce(func.sum(ApiCallBucket.calls), 0)).where(
                    ApiCallBucket.quota_group == group
                )
            ).scalar_one()
        )


def rows(factory: sessionmaker[Session], group: str = GROUP) -> int:
    with factory() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(ApiCallBucket)
                .where(ApiCallBucket.quota_group == group)
            ).scalar_one()
        )


class TestBuckets:
    def test_two_calls_in_one_minute_share_a_row(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """Row count is bounded by elapsed minutes, not by traffic."""
        g, factory = guard
        g.reserve(GROUP, "news", now=NOW)
        g.reserve(GROUP, "news", now=NOW.replace(second=59))

        assert rows(factory) == 1
        assert total(factory) == 2

    def test_different_endpoints_split_rows_but_share_the_budget(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """Endpoint is for attribution. The cap is the group's.

        This is the ledger half of the reason quotas are grouped: news and
        blog can be told apart afterwards, and neither gets its own 12,500.
        """
        g, factory = guard
        g.reserve(GROUP, "news", now=NOW)
        g.reserve(GROUP, "blog", now=NOW)

        assert rows(factory) == 2
        assert g.remaining(GROUP, now=NOW)["test_daily"] == 3

    def test_a_bucket_past_the_window_no_longer_counts(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        g, _ = guard
        g.reserve(GROUP, "news", calls=5, now=NOW - timedelta(hours=25))

        assert g.remaining(GROUP, now=NOW)["test_daily"] == 5
        # Still inside the 31-day ceiling, which is the point of having both.
        assert g.remaining(GROUP, now=NOW)["test_monthly"] == 3


class TestTheBudgetIsNeverExceeded:
    def test_the_last_allowed_call_goes_through(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        g, factory = guard
        for _ in range(5):
            g.reserve(GROUP, "news", now=NOW)

        assert total(factory) == 5
        assert g.remaining(GROUP, now=NOW)["test_daily"] == 0

    def test_the_next_one_is_refused_and_writes_nothing(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """The guarantee, stated as a test: refusal leaves the ledger alone.

        A refusal that still recorded its attempt would push the total past
        budget, which is precisely the state this whole mechanism exists to
        make unreachable.
        """
        g, factory = guard
        for _ in range(5):
            g.reserve(GROUP, "news", now=NOW)
        before = total(factory)

        with pytest.raises(QuotaExhausted) as caught:
            g.reserve(GROUP, "news", now=NOW)

        assert total(factory) == before == 5
        assert caught.value.quota.key == "test_daily"
        assert caught.value.spent == 5
        assert caught.value.allowed == 5

    def test_a_batch_that_would_overshoot_is_refused_whole(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """All or nothing. A partial reservation would count against one
        quota and not another, leaving the two disagreeing."""
        g, factory = guard
        g.reserve(GROUP, "news", calls=3, now=NOW)

        with pytest.raises(QuotaExhausted):
            g.reserve(GROUP, "news", calls=3, now=NOW)

        assert total(factory) == 3

    def test_the_tighter_quota_is_the_one_that_binds(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """Spend the day window down but stay inside the 31-day one.

        Both cover the group, so the refusal has to name whichever ran out
        first rather than whichever happens to be checked first.
        """
        g, _ = guard
        g.reserve(GROUP, "news", calls=5, now=NOW)

        with pytest.raises(QuotaExhausted) as caught:
            g.reserve(GROUP, "news", now=NOW)

        assert caught.value.quota.key == "test_daily"

    def test_the_monthly_ceiling_binds_once_the_day_has_rolled(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """Spread spending over days so only the long window still sees it.

        Oldest first, because that is the only order time actually runs in.
        Reserving backwards would have each call see buckets dated after it,
        which `spent_since` counts on purpose but which no real run produces.
        """
        g, _ = guard
        for day in range(4, 0, -1):
            g.reserve(GROUP, "news", calls=2, now=NOW - timedelta(days=day))

        with pytest.raises(QuotaExhausted) as caught:
            g.reserve(GROUP, "news", now=NOW)

        assert caught.value.quota.key == "test_monthly"
        assert caught.value.spent == 8

    def test_spending_dated_after_now_still_counts(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """`spent_since` bounds the window below and not above, deliberately.

        No real run writes a bucket dated later than the moment it is asking
        about; clock skew is the only way it happens, and if it does, counting
        that spending is the overcount. Ignoring it would be an undercount,
        which is the direction that lets a budget be exceeded.
        """
        g, _ = guard
        g.reserve(GROUP, "news", calls=4, now=NOW)

        assert g.remaining(GROUP, now=NOW - timedelta(hours=1))["test_daily"] == 1

    def test_a_refusal_says_when_headroom_returns(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """SKIPPED is only useful if the detail says what would change it."""
        g, _ = guard
        spent_at = NOW - timedelta(hours=20)
        g.reserve(GROUP, "news", calls=5, now=spent_at)

        with pytest.raises(QuotaExhausted) as caught:
            g.reserve(GROUP, "news", now=NOW)

        assert caught.value.retry_after is not None
        assert caught.value.retry_after == spent_at.replace(second=0, microsecond=0) + timedelta(
            hours=24
        )
        assert "test_daily" in str(caught.value)

    def test_groups_do_not_spend_each_other_s_budget(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        g, _ = guard
        g.reserve(GROUP, "news", calls=5, now=NOW)

        g.reserve(OTHER, "keyword_search", now=NOW)

        assert g.remaining(OTHER, now=NOW)["test_other"] == 1


class TestAccountingOutlivesTheCollector:
    def test_a_reservation_survives_the_collector_rolling_back(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """The reason the guard does not share the collector's session.

        The calls really went out. A rollback that erased them would leave the
        ledger believing we had headroom we had already spent — an undercount,
        which is the one direction that lets a budget be exceeded.
        """
        g, factory = guard

        with factory() as collector_session:
            g.reserve(GROUP, "news", calls=2, now=NOW)
            collector_session.execute(text("SELECT 1"))
            collector_session.rollback()

        assert total(factory) == 2

    def test_reserving_does_not_commit_the_collector_s_pending_work(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """The separation runs both ways: the guard must not flush someone
        else's half-finished writes as a side effect of counting a call."""
        g, factory = guard

        with factory() as collector_session:
            collector_session.execute(
                text(
                    "INSERT INTO api_call_bucket (quota_group, endpoint, minute_start, calls) "
                    "VALUES (:g, 'pending', :m, 99)"
                ),
                {"g": OTHER, "m": NOW.replace(second=0, microsecond=0)},
            )
            g.reserve(GROUP, "news", now=NOW)
            collector_session.rollback()

        assert total(factory, GROUP) == 1
        assert total(factory, OTHER) == 0


class TestHousekeeping:
    def test_pruning_drops_only_what_is_past_retention(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """Retention is measured back from the newest bucket, not from now."""
        g, factory = guard
        g.reserve(GROUP, "news", now=NOW - timedelta(days=50))
        g.reserve(GROUP, "news", now=NOW - timedelta(days=5))

        removed = g.prune(keep=timedelta(days=40), now=NOW)

        assert removed == 1
        assert rows(factory) == 1

    def test_a_clock_that_jumped_forward_cannot_delete_live_history(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """Why retention follows the data rather than the present.

        A resumed VM or a large NTP correction can put the host clock days
        ahead. Pruning against that clock would call every real bucket older
        than retention and delete spending that all six windows still count —
        turning a clock error into an exceeded cap. Anchoring on the newest
        bucket makes the jump cost nothing.
        """
        g, factory = guard
        g.reserve(GROUP, "news", calls=4, now=NOW)

        removed = g.prune(keep=timedelta(days=40), now=NOW + timedelta(days=365))

        assert removed == 0
        assert total(factory) == 4

    def test_remaining_costs_no_quota(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """Checking the budget must not spend it, or the CLI could not be used."""
        g, factory = guard
        g.remaining(GROUP, now=NOW)
        g.report(now=NOW)

        assert rows(factory) == 0

    def test_an_unregistered_group_is_a_programming_error(
        self, guard: tuple[QuotaGuard, sessionmaker[Session]]
    ) -> None:
        """Not a refusal. A call with no quota behind it is a missing entry in
        the plan, and silently allowing it would be a hole in the guarantee."""
        g, _ = guard

        with pytest.raises(ValueError, match="no quota registered"):
            g.reserve("never_registered", "news", now=NOW)
