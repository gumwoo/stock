"""The strategy's policy, in one place, so two callers cannot drift apart.

Weights, required factors, thresholds and the missing-factor rule all decide
what a score means. They lived in `scoring_service`, which is fine while one
caller exists — but a backtest that redeclared them would be measuring a
different strategy from the one the system runs, and the difference would be
invisible: both produce scores, both look plausible, and nothing compares them.

So they live here, in the pure layer both paths already depend on. The live
scorer and the backtest strategy import the same names, and a change to the
rule reaches both or neither.
"""

from __future__ import annotations

from dataclasses import replace

from app.core.types import Availability, Engine, Factor, MissingFactorPolicy
from app.scoring.availability import resolve_availability
from app.scoring.combine import Thresholds

STRATEGY_VERSION = "v0.3-cross-sectional-fundamental"

# The universe a fundamental rank is taken within. Market-scoped, because
# currency, accounting standard and filing source already split on market, and
# because a distribution of multiples in Seoul is not the one in New York.
#
# Sector would be closer to what a rank is supposed to mean. This watchlist
# holds one or two names in most sectors, and `percentile_rank` over a single
# peer returns 50 for everyone in it, so sector scoping waits for a universe
# that can carry it.
#
# It is part of the strategy version: the same filings scored against a
# different peer group produce different signals, which is why a backtest run
# records the universe it used alongside its other coordinates.
PEER_GROUP = "market"

# The base judgement layer. Sentiment is deliberately absent: it is an event
# overlay with a different half-life, not a weighted factor, so it never
# enters this sum. Portfolio joins in Phase 5.
WEIGHTS: dict[Engine, float] = {
    Engine.TECHNICAL: 0.6,
    Engine.FUNDAMENTAL: 0.4,
}

# Only technical is required. Fundamentals are genuinely unavailable for some
# instruments and eras, and abstaining for the whole of a market's history
# would be a policy nobody chose.
REQUIRED: frozenset[Engine] = frozenset({Engine.TECHNICAL})

POLICY = MissingFactorPolicy.ABSTAIN

THRESHOLDS = Thresholds()

# How much price history the rule reads. Shared for the same reason the
# weights are: the live scorer and the backtest must hand their engines the
# same window, or "the same rule" stops being true the moment an indicator
# starts reaching further back than the shorter of the two.
#
# It was 250 live and 260 in the backtest. Nothing differed today — the
# longest indicator looks back 60 — but the divergence was invisible and
# would have stayed invisible until something changed.
SCORING_HISTORY_BARS = 250


def apply_freshness(factor: Factor, *, policy: MissingFactorPolicy, required: bool) -> Factor:
    """Let the freshness verdict actually reduce the factor's weight.

    Engines report what they could compute; they do not judge whether the data
    behind it is current enough to use. That decision belongs to the strategy.

    Without this step the whole freshness chain was computed and then ignored —
    a factor could be marked STALE and still contribute at full weight, which
    made the provenance shown in the UI a decoration rather than a control.
    """
    if factor.availability is Availability.UNAVAILABLE:
        return factor

    verdict = resolve_availability(
        factor.engine,
        provenance=factor.provenance,
        requested_weight=factor.requested_weight,
        policy=policy,
        is_required=required,
    )
    if verdict.availability is Availability.AVAILABLE:
        return factor

    return replace(
        factor,
        availability=verdict.availability,
        effective_weight=verdict.effective_weight,
        availability_reason=verdict.reason,
    )
