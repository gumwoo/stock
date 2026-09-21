"""Orchestrating a backtest: resolve the snapshot, check the ground, run.

The engine is pure and cannot ask the database what data exists, which is
correct — it is what keeps the point-in-time filters unbypassable. But somebody
has to ask, because a run whose period reaches outside the data does not fail.
It simulates nothing for part of the span and then reports the result as a
full-period return.

Live, before this existed: a request for 2020-01-02 to 2026-09-18 against two
years of history returned `sessions = 1651` with an equity curve of 489 points
beginning in 2024. Every number in it was arithmetically correct and described
a different period from the one asked for.

This refuses instead. Silently trimming the window to what happens to be
stored would be the more convenient behaviour and the less honest one: the
caller asked a question about 2020, and the answer is that we cannot answer it,
not a differently-scoped answer that looks like the one requested.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.backtest import engine as bt
from app.backtest import strategies
from app.backtest import walkforward as wf
from app.backtest.engine import BacktestResult, CostModel, MarketData, Strategy
from app.backtest.execution import ExecutionModel
from app.backtest.metrics import Performance, summarise
from app.backtest.pit_repository import PitReader, coverage, snapshot_now
from app.backtest.walkforward import SampleType
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.core.types import Bar, Interval, StrategyDefinition
from app.engines.fundamental import FundamentalSnapshot
from app.models import Instrument
from app.models.backtest import BacktestRun, BacktestWindow
from app.models.fundamental import FundamentalSource
from app.repositories import backtest_repo, fundamental_repo
from app.services import fundamental_service


class BacktestWindowError(Exception):
    """The requested period is not one the stored data can answer."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    """What to simulate. Every field is part of the run's identity."""

    instrument_id: int
    start: date
    end: date
    interval: Interval = Interval.DAY_1
    starting_cash: Decimal = Decimal("10000000")
    costs: CostModel | None = None
    execution_model: ExecutionModel = ExecutionModel.NEXT_OPEN
    bar_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """A finished run, with the coordinates needed to reproduce it."""

    result: BacktestResult
    data_snapshot_at: datetime
    coverage_start: date
    coverage_end: date


class ScoringReader:
    """A `PitReader` that can also answer for fundamentals.

    The reader lives in the repository layer and the snapshot is built by a
    service, so the two cannot meet there without a cycle. They meet here,
    which is also the only layer that holds both a session and a strategy.

    Every lookup is bound by the reader's own instant and snapshot, so a
    strategy that asks about financials is under exactly the same
    point-in-time rules as one that asks about prices.
    """

    __slots__ = ("_asked", "_reader", "_session", "_source")

    def __init__(
        self,
        session: Session,
        reader: PitReader,
        source: FundamentalSource | None,
        asked: list[bool] | None = None,
    ) -> None:
        self._session = session
        self._reader = reader
        self._source = source
        # Shared with every reader this one spawns, so "did the strategy read
        # financials" survives the `at()` calls the engine makes each session.
        self._asked = [] if asked is None else asked

    @property
    def read_fundamentals(self) -> bool:
        """Whether anything actually asked for financials during the run.

        Checking coverage unconditionally would refuse a moving-average run
        for lacking data it never wanted. Checking a flag the caller sets
        would be a flag somebody forgets. Recording the call itself is exact
        and cannot be left out of a new strategy by accident.
        """
        return bool(self._asked)

    @property
    def asof(self) -> datetime:
        return self._reader.asof

    def at(self, asof: datetime) -> ScoringReader:
        return ScoringReader(self._session, self._reader.at(asof), self._source, self._asked)

    def bars(self, instrument_id: int, interval: Interval, *, limit: int = 250) -> list[Bar]:
        return self._reader.bars(instrument_id, interval, limit=limit)

    def opening_price(self, instrument_id: int, interval: Interval) -> Decimal | None:
        return self._reader.opening_price(instrument_id, interval)

    def fundamentals(
        self, instrument_id: int, *, price: float, currency: str
    ) -> FundamentalSnapshot:
        self._asked.append(True)
        return fundamental_service.build_snapshot(
            self._session,
            instrument_id,
            asof=self._reader.asof,
            price=price,
            currency=currency,
            ingested_before=self._reader.data_snapshot_at,
            source=self._source,
        )


# Which source's filings each market's fundamentals come from. A dollar series
# and a won series must never merge, so the source travels with every lookup.
FUNDAMENTAL_SOURCE: dict[Market, FundamentalSource] = {
    Market.KR: FundamentalSource.DART,
    Market.US: FundamentalSource.SEC,
}

CURRENCY: dict[Market, str] = {Market.KR: "KRW", Market.US: "USD"}


def reader_for(session: Session, instrument: Instrument, snapshot: datetime) -> ScoringReader:
    """The data a strategy sees, for this instrument, under this snapshot."""
    return ScoringReader(
        session,
        PitReader(session, data_snapshot_at=snapshot),
        FUNDAMENTAL_SOURCE.get(instrument.market),
    )


def _assert_fundamentals_cover(
    session: Session,
    instrument: Instrument,
    *,
    start: date,
    snapshot: datetime,
) -> None:
    """Refuse a period that reaches back before the financials do.

    Price coverage was checked from the start; this was not, and the gap is
    not cosmetic. Under the current policy the fundamental factor carries 0.4
    of the weight and the threshold scales to whatever participates, so an
    era with no filings is one where the rule can hold or exit but never
    enter — measured, not assumed.

    Live consequence, which is why this exists: Samsung had prices from
    2016-09 and DART filings only from 2023-03, because the collector's
    default reaches back five years. A ten-year run therefore spent six and a
    half of those years structurally unable to buy, and reported +608% as
    though that were a verdict on the strategy.
    """
    source = FUNDAMENTAL_SOURCE.get(instrument.market)
    if source is None:
        return

    # `coverage_start` is the earliest *filing* date, while a fact becomes
    # readable at the following session's open. The check is therefore lenient
    # by one session, which is the right direction: it refuses eras the data
    # cannot speak to and never refuses one it can.
    begins = fundamental_repo.coverage_start(
        session, instrument.instrument_id, source=source, ingested_before=snapshot
    )
    if begins is None:
        raise BacktestWindowError(
            f"{instrument.name} has no {source} filings under this snapshot, but the "
            "strategy read financials. Every session would score on technicals "
            "alone, which is a different rule from the one being measured"
        )
    if start < begins:
        raise BacktestWindowError(
            f"the strategy read financials, but {instrument.name} has {source} filings "
            f"only from {begins} and the run starts {start}. The earlier part would "
            "score on technicals alone — a different rule, reported as the same one. "
            "Collect further back, or start the run at the coverage boundary"
        )


def execute(
    session: Session,
    strategy: Strategy,
    request: RunRequest,
    *,
    data_snapshot_at: datetime | None = None,
    require_complete_sessions: bool = True,
) -> RunOutcome:
    """Run `strategy` over `request`, refusing periods the data cannot cover.

    The snapshot is taken once, here, and used for both the coverage check and
    the simulation. Taking it twice would let a collection landing in between
    widen the data under a run whose bounds were already decided.

    Args:
        require_complete_sessions: refuse when a session inside the window
            produced no bar of its own. Default true. Such a session is marked
            at the last price that printed, which is right for a halt and
            wrong for a stretch the collector missed, and only the caller
            knows which they are looking at. Passing false proceeds anyway —
            deliberately, and the count stays on the result either way, so the
            decision is visible rather than absorbed.
    """
    instrument = session.get(Instrument, request.instrument_id)
    if instrument is None:
        raise BacktestWindowError(f"no instrument {request.instrument_id}")

    if request.end < request.start:
        raise BacktestWindowError(f"end {request.end} precedes start {request.start}")

    snapshot = data_snapshot_at if data_snapshot_at is not None else snapshot_now(session)

    span = coverage(session, request.instrument_id, request.interval, data_snapshot_at=snapshot)
    if span is None:
        raise BacktestWindowError(
            f"no {request.interval} data for {instrument.name} under the snapshot "
            f"{snapshot.isoformat()}; there is nothing to simulate"
        )

    calendar = MarketCalendar(instrument.market)

    # Compare sessions, not the dates that were typed. A window is asked for
    # in ordinary dates — "2025", "Q1", "through the end of November" — and
    # those boundaries land on weekends and holidays constantly. The run only
    # ever touches the sessions inside them, so a Sunday `end` one day past
    # the final session asks for nothing that is missing, and refusing it
    # would be refusing a question we can answer. Walk-forward makes this the
    # normal case rather than the odd one: every window boundary is a month,
    # quarter or year end.
    requested = calendar.sessions_between(request.start, request.end)
    if not requested:
        raise BacktestWindowError(
            f"no {instrument.market} trading sessions between {request.start} "
            f"and {request.end}; there is nothing to simulate"
        )

    first, last = span
    if requested[0] < first or requested[-1] > last:
        raise BacktestWindowError(
            f"requested {request.start}..{request.end}, which covers sessions "
            f"{requested[0]}..{requested[-1]}, but {instrument.name} has "
            f"{request.interval} data only for {first}..{last}. Trimming the window "
            "silently would report a shorter simulation as a full-period result"
        )
    data = reader_for(session, instrument, snapshot)
    result = bt.run(
        strategy,
        data,
        instrument_id=request.instrument_id,
        calendar=calendar,
        start=request.start,
        end=request.end,
        interval=request.interval,
        starting_cash=request.starting_cash,
        costs=request.costs,
        execution_model=request.execution_model,
        bar_minutes=request.bar_minutes,
    )

    # After the run, not before it, because the question is what the strategy
    # actually read and only running it answers that. A refused ten-year run
    # therefore does its work first — the cost of a gate that cannot be
    # bypassed by forgetting to declare something.
    if data.read_fundamentals:
        _assert_fundamentals_cover(session, instrument, start=requested[0], snapshot=snapshot)

    if require_complete_sessions and not result.simulated_full_period:
        # Coverage is judged on the outer dates, so a gap inside them — a
        # suspension, a collection that missed a stretch, or a session our
        # calendar believes in and the exchange did not — gets past the check
        # above. It still means the reported period is not the simulated one.
        missing = result.sessions_without_data
        shown = ", ".join(str(d) for d in missing[:5])
        raise BacktestWindowError(
            f"{len(missing)} of {result.sessions} sessions in {request.start}.."
            f"{request.end} produced no bar for {instrument.name} ({shown}"
            f"{', ...' if len(missing) > 5 else ''}); they were marked at the "
            "last price that printed, so the run would report a period it did "
            "not fully simulate. Pass require_complete_sessions=False to accept this"
        )

    return RunOutcome(
        result=result,
        data_snapshot_at=snapshot,
        coverage_start=first,
        coverage_end=last,
    )


StrategyFitter = Callable[[MarketData, int, date, date], StrategyDefinition]
"""Chooses a strategy from a training period.

Receives a `MarketData` reader confined to that period at both ends, not a
database session. The difference is the whole guarantee. An earlier version
passed the session and claimed the fitter could not look ahead because the
evaluation dates were not among its arguments — which was true and irrelevant,
since a query returns everything. Measured against Samsung, every fitter call
could read all 488 bars, including the 60 reserved as a holdout.

The ceiling makes the evaluation period, the holdout and any later backfill
unreachable rather than merely unmentioned; the floor is what makes a rolling
split actually roll. The dates are still passed, because a fitter needs to
know what period it is fitting.

It returns a `StrategyDefinition` rather than a live strategy, so what each
fold actually chose is recorded alongside the fold's result. A run that stores
only the fitter's name cannot answer why window 3 behaved as it did.
"""


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """What was run, in a form that can be stored and rebuilt.

    Exactly one of `definition` and `fit` is given. A fixed rule and a rule
    chosen per window are different experiments, and a run is one of them
    throughout rather than whichever the caller passed most recently.

    For a fixed run the version comes *from* the definition rather than beside
    it. An earlier version let a caller write both, and they could disagree
    without anything failing: a spec could run 10/30 while recording 20/60,
    and two specs sharing one version could behave differently. Neither broke
    a backtest; both broke the reproducibility a stored run exists to provide.
    """

    definition: StrategyDefinition | None = None
    fit: StrategyFitter | None = None
    fitter_version: str | None = None

    def __post_init__(self) -> None:
        if (self.definition is None) == (self.fit is None):
            raise ValueError(
                "give exactly one of definition or fit: a fixed rule and a fitted "
                "one are different experiments"
            )
        if self.fit is not None and not (self.fitter_version or "").strip():
            raise ValueError(
                "a fitted run needs a fitter_version: the fitter is what chose "
                "the parameters, so it is the thing that has to be reproducible"
            )

    @property
    def fitted(self) -> bool:
        return self.fit is not None

    @property
    def version(self) -> str:
        """What a stored row records, and what a later reader matches on."""
        if self.definition is not None:
            return self.definition.version
        assert self.fitter_version is not None  # guaranteed above
        return self.fitter_version


@dataclass(frozen=True, slots=True)
class WindowResult:
    """One window's two measurements, and what stood behind them."""

    index: int
    sample_type: SampleType
    start: date
    end: date
    chosen: StrategyDefinition
    performance: Performance | None
    sessions: int
    trades: int
    abstained: int
    without_data: int
    unfilled: int


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """Every window, in and out of sample, with the caveats attached.

    `fitted` is the one to read first. When no fitting step was supplied, the
    same strategy ran on both sides of every split, and the in-sample figures
    are simply "how it did over those months" — not the period a rule was
    tuned on. An IN/OUT gap then says nothing about overfitting, and the UI
    must not present it as if it did. The flag is here so that claim cannot be
    made by accident.

    Each window is an independent run starting from cash. The evaluation dates
    tile the timeline, but the portfolios do not continue across them: window 1
    does not inherit window 0 position. These are per-fold comparisons, and
    stitching their curves together would depict a portfolio nobody held.
    """

    windows: tuple[WindowResult, ...]
    spec: StrategySpec
    request: RunRequest
    code: backtest_repo.CodeVersion
    started_at: datetime
    data_snapshot_at: datetime
    train_sessions: int
    eval_sessions: int
    anchored: bool
    require_complete_sessions: bool
    holdout_start: date | None = None
    holdout_end: date | None = None

    @property
    def fitted(self) -> bool:
        return self.spec.fitted

    def of(self, sample: SampleType) -> tuple[WindowResult, ...]:
        return tuple(w for w in self.windows if w.sample_type is sample)

    @property
    def evaluation_span(self) -> tuple[date, date] | None:
        """How much history the out-of-sample figures speak for."""
        out = self.of(SampleType.OUT_OF_SAMPLE)
        if not out:
            return None
        return out[0].start, out[-1].end


def walk_forward(
    session: Session,
    spec: StrategySpec,
    request: RunRequest,
    *,
    train_sessions: int,
    eval_sessions: int,
    step_sessions: int | None = None,
    anchored: bool = False,
    holdout_sessions: int = 0,
    data_snapshot_at: datetime | None = None,
    require_complete_sessions: bool = True,
) -> WalkForwardReport:
    """Run rolling train/evaluate splits across the requested period.

    One snapshot covers every window. Taking a fresh one per window would let
    a collection landing mid-run widen the data under the later windows only,
    which is the sort of difference that reads as the strategy improving.

    The spec says whether the rule is fixed or fitted per window; a run is one
    or the other throughout.
    """
    instrument = session.get(Instrument, request.instrument_id)
    if instrument is None:
        raise BacktestWindowError(f"no instrument {request.instrument_id}")

    # Captured before anything runs. Resolving it at persist time would
    # record whatever HEAD happened to be once a long run finished, which is
    # not necessarily the code that produced the numbers.
    code = backtest_repo.resolve_commit()
    started_at = utc_now()

    snapshot = data_snapshot_at if data_snapshot_at is not None else snapshot_now(session)
    calendar = MarketCalendar(instrument.market)
    sessions = calendar.sessions_between(request.start, request.end)

    split = wf.generate(
        sessions,
        train_sessions=train_sessions,
        eval_sessions=eval_sessions,
        step_sessions=step_sessions,
        anchored=anchored,
        holdout_sessions=holdout_sessions,
    )

    results: list[WindowResult] = []
    for window in split.windows:
        if spec.fit is None:
            definition = spec.definition
            assert definition is not None  # guaranteed by StrategySpec
        else:
            # Confined to the training period at both ends. The ceiling keeps
            # the evaluation window, the holdout and any later backfill
            # unreadable rather than merely unmentioned. The floor is what
            # makes a rolling split roll: without it the fitter reads back to
            # the first bar in the database, and `anchored` becomes the only
            # behaviour the fitter has.
            training_view = PitReader(session, data_snapshot_at=snapshot).windowed(
                not_before=calendar.session_open(window.train_start),
                not_after=calendar.session_close(window.train_end),
            )
            definition = spec.fit(
                training_view,
                request.instrument_id,
                window.train_start,
                window.train_end,
            )
        chosen = strategies.build(definition)
        for sample, lo, hi in (
            (SampleType.IN_SAMPLE, window.train_start, window.train_end),
            (SampleType.OUT_OF_SAMPLE, window.eval_start, window.eval_end),
        ):
            outcome = execute(
                session,
                chosen,
                replace(request, start=lo, end=hi),
                data_snapshot_at=snapshot,
                require_complete_sessions=require_complete_sessions,
            )
            result = outcome.result
            results.append(
                WindowResult(
                    index=window.index,
                    sample_type=sample,
                    start=lo,
                    end=hi,
                    chosen=definition,
                    performance=summarise(result.equity_curve, result.trades),
                    sessions=result.sessions,
                    trades=len(result.trades),
                    abstained=len(result.abstained_sessions),
                    without_data=len(result.sessions_without_data),
                    unfilled=len(result.unfilled),
                )
            )

    return WalkForwardReport(
        windows=tuple(results),
        spec=spec,
        request=request,
        code=code,
        started_at=started_at,
        data_snapshot_at=snapshot,
        train_sessions=train_sessions,
        eval_sessions=eval_sessions,
        anchored=anchored,
        require_complete_sessions=require_complete_sessions,
        holdout_start=split.holdout_start,
        holdout_end=split.holdout_end,
    )


class HoldoutError(Exception):
    """The reserved tail cannot be evaluated as asked."""


def evaluate_holdout(
    session: Session,
    report: WalkForwardReport,
) -> WindowResult:
    """Evaluate the reserved tail, once, after every choice has been made.

    **Deliberately not part of `walk_forward`.** If the holdout were scored on
    every call, anyone adjusting window lengths or trying a different rule
    would see its number each time, and after a few iterations it would be as
    thoroughly fitted as the training data — by eye rather than by code, which
    is harder to notice and no less real. A holdout survives only while
    looking at it is a separate, deliberate act.

    **It takes the report and nothing else.** An earlier version accepted the
    strategy, the request and the fitter again, and claimed the report kept
    the run from drifting. That was true only of the snapshot, the training
    length and the anchoring. Everything else could be swapped, and the two
    ways that went wrong were not subtle:

        AAPL run, fitted, 10,000 cash, 5bp     honest holdout      -1.72%
        scored against Samsung instead                            -16.63%
        scored with fit=None instead                              +21.91%

    A different instrument was accepted as the conclusion of the Apple
    experiment, and dropping the fitter moved the number 23 points in the
    flattering direction. Neither is reachable now: the request, the spec and
    the split all come from the report, so the only thing a caller can vary is
    which report they pass.
    """
    if report.holdout_start is None or report.holdout_end is None:
        raise HoldoutError(
            "this run reserved no holdout; pass holdout_sessions to walk_forward "
            "before the choices are made, not after"
        )

    request = report.request
    instrument = session.get(Instrument, request.instrument_id)
    if instrument is None:
        raise BacktestWindowError(f"no instrument {request.instrument_id}")

    calendar = MarketCalendar(instrument.market)
    sessions = calendar.sessions_between(request.start, request.end)

    opening = sessions.index(report.holdout_start)
    if opening == 0:
        raise HoldoutError("the holdout starts at the first session; nothing precedes it")

    final_train_end = sessions[opening - 1]
    final_train_start = (
        sessions[0] if report.anchored else sessions[max(0, opening - report.train_sessions)]
    )

    definition = report.spec.definition
    if report.spec.fit is not None:
        # Same confinement as every other fitting: it may read up to the last
        # session before the holdout opens and no further.
        training_view = PitReader(session, data_snapshot_at=report.data_snapshot_at).windowed(
            not_before=calendar.session_open(final_train_start),
            not_after=calendar.session_close(final_train_end),
        )
        definition = report.spec.fit(
            training_view, request.instrument_id, final_train_start, final_train_end
        )
    assert definition is not None  # guaranteed by StrategySpec
    chosen = strategies.build(definition)

    outcome = execute(
        session,
        chosen,
        replace(request, start=report.holdout_start, end=report.holdout_end),
        data_snapshot_at=report.data_snapshot_at,
        require_complete_sessions=report.require_complete_sessions,
    )
    result = outcome.result

    return WindowResult(
        index=len(report.of(SampleType.OUT_OF_SAMPLE)),
        sample_type=SampleType.HOLDOUT,
        start=report.holdout_start,
        end=report.holdout_end,
        chosen=definition,
        performance=summarise(result.equity_curve, result.trades),
        sessions=result.sessions,
        trades=len(result.trades),
        abstained=len(result.abstained_sessions),
        without_data=len(result.sessions_without_data),
        unfilled=len(result.unfilled),
    )


def fit_trace_fingerprint(report: WalkForwardReport) -> str:
    """A digest of what every window actually ran, in order.

    The run header cannot describe a fitted experiment. Each window chose its
    own definition, so the header carries a placeholder — the kind, the
    fitter's version and a window count — and two fitters sharing a version
    produce identical headers however differently they behave:

        stored   win 0 MA 10/30 · win 1 MA 15/40 · win 2 MA 20/50 · …
        impostor win 0 MA 20/60 · win 1 MA 20/60 · win 2 MA 20/60 · …

        both header as moving_average_cross@ma-grid@v1 {fitted: true,
        windows: 5}

    The per-window rows already record the choices; this puts them in the
    header too, as one value, so the identity check sees them without needing
    to read back and compare row by row. A fixed run gets one as well — there
    the choices are constant, and the digest then pins the window boundaries,
    which costs nothing and keeps the column uniform.
    """
    return fit_trace_fingerprint_of(
        (w.index, w.sample_type, w.start, w.end, w.chosen.canonical) for w in report.windows
    )


def fit_trace_fingerprint_of(rows: Iterable[tuple[object, ...]]) -> str:
    """The digest itself, over rows in whatever form the caller holds them.

    Split out so a stored run can be digested from its own window rows and
    compared against its header — the only way to tell that the rows are the
    ones the header was written for.
    """
    trace = "\n".join("|".join(str(part) for part in row) for row in rows)
    return hashlib.sha256(trace.encode("utf-8")).hexdigest()[:16]


def experiment_fields(report: WalkForwardReport) -> dict[str, object]:
    """Every stored coordinate that makes a run the experiment it is.

    Used both to write a run and to check that a later holdout belongs to it,
    so the two cannot drift. The previous version listed the fields to compare
    by hand and missed six of them, which let a MA 10/30 run at 5bp have its
    holdout recorded as buy-and-hold at zero cost:

        stored run #159: moving_average_cross@ma-10-30@v1 {short:10, long:30}
                         commission=5bp execution=NEXT_OPEN
        accepted holdout: buy_and_hold@buy-and-hold@v1  return -0.210

    Deriving both from one function means a column added later is compared
    from the moment it is stored, rather than the next time someone remembers.

    Code and timing are deliberately absent: `git_commit_sha`, `git_dirty`,
    `started_at` and `ingested_at` describe when and where a run happened, not
    which experiment it was.
    """
    request = report.request
    costs = request.costs if request.costs is not None else CostModel()
    definition = report.spec.definition or _fitted_placeholder(report)

    return {
        "instrument_id": request.instrument_id,
        "strategy_kind": definition.kind,
        "strategy_version": definition.version,
        "strategy_params": dict(definition.params),
        "strategy_fingerprint": definition.fingerprint,
        "fitter_version": report.spec.fitter_version,
        "fit_trace_fingerprint": fit_trace_fingerprint(report),
        "data_snapshot_at": report.data_snapshot_at,
        "interval": request.interval,
        "period_start": request.start,
        "period_end": request.end,
        "starting_cash": request.starting_cash,
        "commission_bps": costs.commission_bps,
        "slippage_bps": costs.slippage_bps,
        "min_commission": costs.min_commission,
        "execution_model": request.execution_model.value,
        "bar_minutes": request.bar_minutes,
        "train_sessions": report.train_sessions,
        "eval_sessions": report.eval_sessions,
        "anchored": report.anchored,
        "holdout_start": report.holdout_start,
        "holdout_end": report.holdout_end,
        "require_complete_sessions": report.require_complete_sessions,
    }


def persist(
    session: Session,
    report: WalkForwardReport,
    *,
    code: backtest_repo.CodeVersion | None = None,
) -> BacktestRun:
    """Store a finished walk-forward with the coordinates that prove it.

    The commit and the start time come from the report, captured when the run
    began. Resolving them here would record the state of the working tree once
    persisting happened to be called — after a long run, potentially a
    different commit from the one that produced the numbers.

    The costs written are the ones that were applied, expanded into numbers.
    Recording "the default cost model" would become a different claim the day
    the default moved, and every stored run would silently reinterpret itself.

    The holdout is not written here. It is not part of a walk-forward — it is
    the separate measurement taken afterwards — and writing a row for it at
    this point would mean the run had one before anyone decided to look.
    """
    run = backtest_repo.save_run(
        session,
        provenance=backtest_repo.RunProvenance(
            code=code or report.code,
            started_at=report.started_at,
        ),
        **experiment_fields(report),
    )

    for window in report.windows:
        _save_window(session, run, window)
    return run


def evaluate_and_persist_holdout(
    session: Session, run: BacktestRun, report: WalkForwardReport
) -> BacktestWindow:
    """Take the final measurement and store it against the run it concludes.

    One call rather than two, because the gap between them was reachable. The
    earlier `persist_holdout(session, run, result)` checked only that the
    result *was* a holdout — not that it came from this run. Reproduced live:
    Samsung's holdout stored on Apple's run, sitting beside the real one,

        run #36 holdout rows: 2
          index=5  2026-06-25..2026-09-18  return +0.219
          index=6  2026-06-26..2026-09-18  return -0.211

    and nothing in the row said which experiment either belonged to.

    The database now forbids two holdouts per run outright. This forbids the
    other half: a holdout from a different experiment taking the one slot. The
    run row and the report are compared on every coordinate that defines the
    experiment, so a mismatch is refused rather than recorded.
    """
    _assert_same_experiment(run, report)
    result = evaluate_holdout(session, report)
    stored = _save_window(session, run, result)

    # Anchor which strategy the final measurement used. A fitted run's last
    # refit need not match any fold's choice, so the fit trace cannot cover
    # it, and without this the holdout row can be rewritten to a different
    # strategy and still replay to its own figures.
    run.holdout_strategy_fingerprint = result.chosen.fingerprint
    session.flush()
    return stored


def _assert_same_experiment(run: BacktestRun, report: WalkForwardReport) -> None:
    """Refuse a report that is not the experiment stored as this run.

    Compares every field `experiment_fields` produces, so the check cannot
    fall behind what is stored.
    """
    mismatches = [
        f"{name} (stored {getattr(run, name)!r}, given {live!r})"
        for name, live in experiment_fields(report).items()
        if getattr(run, name) != live
    ]
    if mismatches:
        raise HoldoutError(
            f"this report is not the run stored as #{run.id}: "
            + "; ".join(mismatches)
            + ". A holdout concludes one experiment; attaching another's would "
            "put a number on the run that nothing in the row explains"
        )


def _save_window(session: Session, run: BacktestRun, window: WindowResult) -> BacktestWindow:
    performance = window.performance
    return backtest_repo.save_window(
        session,
        run,
        window_index=window.index,
        sample_type=window.sample_type,
        period_start=window.start,
        period_end=window.end,
        chosen=window.chosen,
        sessions=window.sessions,
        observations=performance.observations if performance else 0,
        total_return=performance.total_return if performance else None,
        cagr=performance.cagr if performance else None,
        max_drawdown=performance.max_drawdown if performance else None,
        sharpe=performance.sharpe if performance else None,
        win_rate=performance.win_rate if performance else None,
        profit_factor=performance.profit_factor if performance else None,
        trades=window.trades,
        abstained=window.abstained,
        without_data=window.without_data,
        unfilled=window.unfilled,
    )


def _fitted_placeholder(report: WalkForwardReport) -> StrategyDefinition:
    """The run-level definition for a fitted experiment.

    A fitted run has no single strategy — each window chose its own, and those
    are stored on the window rows. The run header records the kind the fitter
    produced and the fitter's version, so the header is never mistaken for a
    fixed rule that was actually run.
    """
    kinds = {w.chosen.kind for w in report.windows}
    kind = kinds.pop() if len(kinds) == 1 else "mixed"
    return StrategyDefinition(
        kind=kind,
        version=report.spec.version,
        params={"fitted": True, "windows": len(report.of(SampleType.OUT_OF_SAMPLE))},
    )
