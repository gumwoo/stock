"""The system's actual judgement rule, as a backtestable strategy.

Everything up to here proved the machinery. A moving-average cross is a
harness: it exercises the point-in-time filters, the execution clock and the
persistence layer, and says nothing about whether this system's own rule is
any good. This is the rule — the same technical and fundamental engines, the
same weights, the same thresholds the live scorer uses.

**The policy is imported, not restated.** `app.scoring.policy` holds the
weights, the required factors, the missing-factor rule and the thresholds, and
both paths read them from there. A backtest that declared its own copy would
be measuring a different strategy from the one the system runs, and the
difference would be invisible: both produce scores, both look plausible, and
nothing compares them.

**Freshness cannot be asked about the past.** The live scorer judges the
fundamental factor on whether the source was checked recently — a fact about
our collectors *now*, not then. Replaying that inside a backtest would leak
today's plumbing into a 2024 decision, which is look-ahead of an unusually
silly kind. Here the point-in-time filter already guarantees that only facts
available at the simulated instant are visible, so the factor is treated as
fresh by construction and the difference is stated rather than hidden.

That is the one place the backtest and the live path deliberately differ, and
it is a difference in what can be known rather than in what is decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.backtest.engine import ScoringData, Signal
from app.core.types import (
    DataProvenance,
    Engine,
    Freshness,
    SignalAction,
)
from app.core.types import Interval as IntervalType
from app.engines.fundamental import FundamentalEngine, FundamentalParams
from app.engines.technical import PriceSeries, TechnicalEngine, TechnicalParams
from app.scoring.combine import Thresholds
from app.scoring.policy import (
    POLICY,
    REQUIRED,
    SCORING_HISTORY_BARS,
    THRESHOLDS,
    WEIGHTS,
    apply_freshness,
)

__all__ = ["REQUIRED", "SCORING_HISTORY_BARS", "THRESHOLDS", "WEIGHTS", "TechnicalFundamental"]


@dataclass(frozen=True, slots=True)
class TechnicalFundamental:
    """Long while the system's own score says buy, flat when it says caution.

    The score is the one the dashboard shows. Turning it into a position needs
    one more decision — which actions mean hold what — and that decision is
    part of the strategy rather than of the scorer, because the scorer's job
    ends at "this is worth a look".

        BUY_INTEREST   enter
        CAUTION        exit
        WATCH          neither: hold whatever is already held
        ABSTAINED      no judgement was made, so none is acted on

    WATCH holding rather than exiting is deliberate. A rule that sold every
    time the score drifted into the middle would trade constantly on noise,
    and the thresholds exist precisely to separate a view from an absence of
    one.
    """

    # `None` means "whatever the shared policy says", resolved when the rule
    # runs rather than when this module is imported. A default written as
    # `THRESHOLDS.buy_interest` would look shared and be a copy: Python
    # evaluates it once, at import, so a later change to the policy would
    # leave this holding the old number with nothing failing.
    buy_interest: float | None = None
    caution: float | None = None
    interval: IntervalType = IntervalType.DAY_1
    currency: str = "KRW"

    @property
    def thresholds(self) -> Thresholds:
        """What turns a score into a position, from the policy unless stated."""
        return Thresholds(
            buy_interest=(
                THRESHOLDS.buy_interest if self.buy_interest is None else self.buy_interest
            ),
            caution=THRESHOLDS.caution if self.caution is None else self.caution,
        )

    def evaluate(self, data: ScoringData, instrument_id: int) -> Signal:
        bars = data.bars(instrument_id, self.interval, limit=SCORING_HISTORY_BARS)
        if not bars:
            return Signal.ABSTAIN

        series = PriceSeries(
            instrument_id=instrument_id,
            closes=tuple(float(b.close) for b in bars),
            volumes=tuple(float(b.volume) for b in bars),
            asof=bars[-1].available_at,
        )

        technical, _ = TechnicalEngine(TechnicalParams()).evaluate(
            series,
            requested_weight=WEIGHTS[Engine.TECHNICAL],
            provenance=_provenance(bars[-1].available_at),
        )

        snapshot = data.fundamentals(
            instrument_id, price=float(bars[-1].close), currency=self.currency
        )
        fundamental, _ = FundamentalEngine(FundamentalParams()).evaluate(
            snapshot,
            requested_weight=WEIGHTS[Engine.FUNDAMENTAL],
            provenance=_provenance(bars[-1].available_at),
            # Ranked against the market as it was at this instant, under the
            # same point-in-time bounds as everything else the reader returns.
            # A run given no universe gets None and scores on the fixed scale,
            # which is what every run stored before this existed did.
            peers=data.peers(),
        )

        factors = tuple(
            apply_freshness(f, policy=POLICY, required=f.engine in REQUIRED)
            for f in (technical, fundamental)
        )
        participating = sum(f.effective_weight for f in factors)
        if participating <= 0:
            return Signal.ABSTAIN

        score = sum(f.contribution for f in factors)
        action = self.thresholds.action_for(score, participating)

        match action:
            case SignalAction.BUY_INTEREST:
                return Signal.ENTER
            case SignalAction.CAUTION:
                return Signal.EXIT
            case SignalAction.ABSTAINED:
                return Signal.ABSTAIN
            case _:
                return Signal.HOLD


def _provenance(asof: datetime) -> DataProvenance:
    """Fresh by construction, because the past cannot be asked.

    The live scorer decides freshness from how recently a collector succeeded.
    That is a fact about our plumbing at this moment, and consulting it while
    simulating 2024 would let today's collector state decide a historical
    trade. What the backtest can guarantee instead is stronger for its
    purpose: the point-in-time reader only returns what was knowable at the
    simulated instant, so nothing stale in the market's sense can be seen at
    all.
    """
    return DataProvenance(
        source_asof=asof,
        source_checked_at=asof,
        data_age=timedelta(0),
        freshness=Freshness.FRESH,
    )
