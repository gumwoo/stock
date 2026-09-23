"""The overlay arithmetic: one event however many outlets, fading, bounded."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.scoring.overlay import EventReading, OverlayParams, cluster, compute_overlay

T = datetime(2026, 9, 23, 12, tzinfo=UTC)
H = timedelta(hours=1)
P = OverlayParams()


def reading(
    n: int,
    *,
    at: datetime = T - H,
    event: str = "SHAREHOLDER_RETURN",
    sentiment: float = 0.8,
    intensity: float = 0.6,
    confidence: float = 0.9,
) -> EventReading:
    return EventReading(
        news_item_id=n,
        available_at=at,
        event_type=event,
        sentiment=sentiment,
        intensity=intensity,
        confidence=confidence,
        title=f"article {n}",
    )


class TestOneEventCountsOnce:
    def test_twenty_outlets_are_one_event(self) -> None:
        """A buyback reported twenty times is still one buyback."""
        one = compute_overlay([reading(1)], asof=T, params=P)
        twenty = compute_overlay(
            [reading(n, at=T - H - n * timedelta(minutes=10)) for n in range(20)], asof=T, params=P
        )
        assert len(twenty.clusters) == 1
        assert twenty.clusters[0].articles == 20
        assert twenty.raw == pytest.approx(one.raw, rel=0.2)

    def test_different_kinds_of_news_are_different_events(self) -> None:
        overlay = compute_overlay([reading(1), reading(2, event="EARNINGS")], asof=T, params=P)
        assert len(overlay.clusters) == 2

    def test_the_same_kind_days_apart_is_a_new_event(self) -> None:
        groups = cluster([reading(1, at=T - 50 * H), reading(2, at=T - H)], P.cluster_window)
        assert [len(g) for g in groups] == [1, 1]

    def test_the_window_runs_from_the_first_article_not_the_last(self) -> None:
        """A steady trickle must not chain into one long event.

        Four articles twenty hours apart: measured from the last article each
        would join the one before and all four become one event; measured
        from the first they pair up.
        """
        trickle = [reading(n, at=T - H - n * 20 * H) for n in range(4)]
        assert [len(g) for g in cluster(trickle, P.cluster_window)] == [2, 2]

    def test_direction_is_weighted_by_confidence(self) -> None:
        overlay = compute_overlay(
            [
                reading(1, sentiment=1.0, confidence=0.9),
                reading(2, sentiment=-1.0, confidence=0.3),
            ],
            asof=T,
            params=P,
        )
        assert overlay.clusters[0].sentiment == pytest.approx((0.9 - 0.3) / 1.2)

    def test_the_event_is_shown_by_its_most_telling_article(self) -> None:
        overlay = compute_overlay(
            [
                reading(1, at=T - 3 * H, intensity=0.2, confidence=0.5),
                reading(2, at=T - 2 * H, intensity=0.7, confidence=0.9),
            ],
            asof=T,
            params=P,
        )
        assert overlay.clusters[0].title == "article 2"


class TestEventsFade:
    def test_weight_halves_every_half_life(self) -> None:
        half = P.half_life("SHAREHOLDER_RETURN")
        fresh = compute_overlay([reading(1, at=T)], asof=T, params=P)
        old = compute_overlay([reading(1, at=T - half)], asof=T, params=P)
        assert old.raw == pytest.approx(fresh.raw / 2)

    def test_a_price_move_fades_faster_than_an_acquisition(self) -> None:
        day = T - 24 * H
        move = compute_overlay([reading(1, at=day, event="PRICE_MOVE")], asof=T, params=P)
        deal = compute_overlay([reading(1, at=day, event="MERGER_ACQUISITION")], asof=T, params=P)
        assert move.clusters[0].decay < deal.clusters[0].decay

    def test_an_event_past_the_horizon_is_gone(self) -> None:
        far = T - P.half_life("PRICE_MOVE") * (P.horizon_half_lives + 1)
        assert (
            compute_overlay([reading(1, at=far, event="PRICE_MOVE")], asof=T, params=P).clusters
            == ()
        )

    def test_news_after_the_moment_is_not_there(self) -> None:
        assert compute_overlay([reading(1, at=T + H)], asof=T, params=P).clusters == ()


class TestBounded:
    def test_no_pile_of_news_passes_the_maximum(self) -> None:
        many = [
            reading(n, event=e, sentiment=1.0, intensity=1.0, confidence=1.0)
            for n, e in enumerate(P.half_lives)
        ]
        overlay = compute_overlay(many, asof=T, params=P)
        assert overlay.raw > 5
        assert overlay.points < P.max_points
        assert overlay.points == pytest.approx(P.max_points * math.tanh(overlay.raw))

    def test_bad_news_is_negative(self) -> None:
        assert compute_overlay([reading(1, sentiment=-0.7)], asof=T, params=P).points < 0

    def test_a_reading_the_model_was_unsure_of_does_not_count(self) -> None:
        overlay = compute_overlay([reading(1, confidence=0.2)], asof=T, params=P)
        assert (overlay.clusters, overlay.readings_dropped) == ((), 1)
