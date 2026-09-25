"""The morning watchlist rule: who is in, why, and in what order."""

from __future__ import annotations

from app.scoring.watchlist import (
    DISCLOSURE_EVENT,
    DISCOVERY_SURGE,
    MAX_MEMBERS,
    NEGATIVE_NEWS_OVERLAY,
    POSITIVE_NEWS_OVERLAY,
    SEARCH_SURGE_REASON,
    TRACKED,
    TRACKED_HIGH_SCORE,
    Seen,
    reasons,
    select_names,
)


class TestReasons:
    def test_every_reason_a_name_has_is_listed(self) -> None:
        seen = Seen(
            instrument_id=1,
            tracked=True,
            overlay_points=3.2,
            has_disclosure_event=True,
            search_surge=2.4,
            discovery_score=4.1,
            last_action="BUY_INTEREST",
        )
        assert reasons(seen) == (
            DISCOVERY_SURGE,
            POSITIVE_NEWS_OVERLAY,
            DISCLOSURE_EVENT,
            SEARCH_SURGE_REASON,
            TRACKED_HIGH_SCORE,
            TRACKED,
        )

    def test_the_thresholds_are_the_systems_own(self) -> None:
        assert reasons(Seen(1, False, overlay_points=0.99)) == ()
        assert reasons(Seen(1, False, overlay_points=-1.0)) == (NEGATIVE_NEWS_OVERLAY,)
        assert reasons(Seen(1, False, search_surge=1.99)) == ()
        assert reasons(Seen(1, False, search_surge=2.0)) == (SEARCH_SURGE_REASON,)

    def test_a_buy_interest_counts_only_for_a_tracked_name(self) -> None:
        assert reasons(Seen(1, False, last_action="BUY_INTEREST")) == ()

    def test_a_name_with_nothing_is_not_chosen(self) -> None:
        assert select_names([Seen(1, False)]).picks == []


class TestOrder:
    def test_more_reasons_first_then_news_then_search_then_discovery(self) -> None:
        pool = [
            Seen(1, True),  # tracked only
            Seen(2, False, overlay_points=1.5),
            Seen(3, False, overlay_points=-4.0),
            Seen(4, False, overlay_points=1.5, search_surge=3.0),
            Seen(5, False, search_surge=2.5),
            Seen(6, False, discovery_score=9.0),
        ]
        ranked = [p.instrument_id for p in select_names(pool).picks]
        assert ranked == [4, 3, 2, 5, 6, 1]

    def test_ranks_are_one_to_n_and_ties_go_by_id(self) -> None:
        picks = select_names([Seen(9, True), Seen(7, True), Seen(8, True)]).picks
        assert [(p.rank, p.instrument_id) for p in picks] == [(1, 7), (2, 8), (3, 9)]

    def test_forty_at_most_and_the_rest_counted(self) -> None:
        pool = [Seen(i, True) for i in range(1, 51)]
        chosen = select_names(pool)
        assert len(chosen.picks) == MAX_MEMBERS
        assert chosen.left_out == 10

    def test_the_same_inputs_give_the_same_list(self) -> None:
        pool = [Seen(i, i % 2 == 0, overlay_points=float(i % 5 - 2)) for i in range(60)]
        assert select_names(pool) == select_names(list(reversed(pool)))


def test_being_tracked_is_why_a_name_is_present_not_why_it_stands_out() -> None:
    # Tracked with big news: one reason that stands out. Untracked with news
    # and a search surge: two. Two stand out more than one.
    tracked = Seen(1, True, overlay_points=4.0)
    untracked = Seen(2, False, overlay_points=1.5, search_surge=2.1)
    assert [p.instrument_id for p in select_names([tracked, untracked]).picks] == [2, 1]
