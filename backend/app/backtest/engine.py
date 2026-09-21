"""The simulation loop.

Event-driven over trading sessions. Each session produces at most one decision,
taken at the close, and any resulting fill happens at the next tradable instant
— never at the close that produced it.

**The engine holds no session and knows no ORM.** It receives its data through
the `MarketData` protocol, which `PitReader` satisfies structurally. That is
not decoration: the point-in-time filters live in the reader, so an engine able
to construct its own queries could bypass them, and CI forbids this module from
importing SQLAlchemy at all. The protocol is how the rule is kept while the
engine still gets data.

**Every fill is checked, including the ones the engine derived itself.** The
check is a few comparisons and it is the only thing standing between a future
refactor and an equity curve that quietly improves. A violation raises; nothing
here corrects a bad instant into a good one, because a correction would let a
strategy break the rule all run and still produce a plausible number.

**A fill that cannot happen does not happen.** If the market has no opening
price at the execution instant — a suspension, a halt, data we never collected
— the order is dropped and counted. Carrying it forward to the next session
would be inventing a trade the strategy never placed, and substituting a
nearby price would be inventing the price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Protocol

from app.backtest.execution import (
    ExecutionModel,
    assert_executable,
    earliest_execution,
)
from app.backtest.metrics import ClosedTrade, EquityPoint
from app.core.calendar import MarketCalendar
from app.core.types import Bar, Interval

# One basis point. Costs are quoted in bps because that is how brokers quote
# them, and because a fraction written as 0.00015 invites a misplaced zero.
BPS = Decimal("0.0001")


class Signal(StrEnum):
    """What a strategy wants at one decision instant."""

    ENTER = "ENTER"
    EXIT = "EXIT"
    HOLD = "HOLD"

    ABSTAIN = "ABSTAIN"
    """The strategy declines to judge — a required factor was unavailable.

    Distinct from HOLD, and the distinction is the whole point of the policy.
    HOLD is a judgement that the current position is right. ABSTAIN is the
    absence of a judgement: no new position is opened, an existing one is left
    alone, and the day stays in the equity curve rather than being deleted from
    it. Dropping such days would be a bias in itself, which is why the
    simulation clock never skips them.
    """


class MarketData(Protocol):
    """What the engine is allowed to ask for.

    Deliberately narrow. `PitReader` satisfies it, and so can a fixture, but
    neither can be widened from in here.
    """

    @property
    def asof(self) -> datetime: ...

    def at(self, asof: datetime) -> MarketData: ...

    def bars(self, instrument_id: int, interval: Interval, *, limit: int = ...) -> list[Bar]: ...

    def opening_price(self, instrument_id: int, interval: Interval) -> Decimal | None: ...


class Strategy(Protocol):
    """A rule that turns readable data into an intention.

    It receives the reader already positioned at the decision instant, so it
    cannot read forward even by accident.
    """

    def evaluate(self, data: MarketData, instrument_id: int) -> Signal: ...


@dataclass(frozen=True, slots=True)
class CostModel:
    """What trading costs, in basis points of notional.

    Slippage is applied against the trade — paying up to buy, down to sell —
    because a simulation that fills at the untouched open is claiming the
    strategy moved no price and crossed no spread. Zero is expressible, and it
    is a claim, not a default worth hiding.
    """

    commission_bps: Decimal = Decimal("5")
    slippage_bps: Decimal = Decimal("5")
    min_commission: Decimal = Decimal("0")

    def fill_price(self, quoted: Decimal, *, buying: bool) -> Decimal:
        drift = quoted * self.slippage_bps * BPS
        return quoted + drift if buying else quoted - drift

    def commission(self, notional: Decimal) -> Decimal:
        return max(notional * self.commission_bps * BPS, self.min_commission)


@dataclass(frozen=True, slots=True)
class Fill:
    """One executed order, with the instants that justify it."""

    instrument_id: int
    decision_at: datetime
    execution_at: datetime
    buying: bool
    quantity: int
    price: Decimal
    commission: Decimal

    @property
    def cash_delta(self) -> Decimal:
        notional = self.price * self.quantity
        return -(notional + self.commission) if self.buying else notional - self.commission


@dataclass(frozen=True, slots=True)
class UnfilledOrder:
    """An order that had no price to fill at, and the reason."""

    instrument_id: int
    decision_at: datetime
    execution_at: datetime
    buying: bool
    reason: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Everything a run produced, ready to be scored or persisted."""

    equity_curve: list[EquityPoint]
    fills: list[Fill]
    trades: list[ClosedTrade]
    unfilled: list[UnfilledOrder]
    abstained_sessions: list[date] = field(default_factory=list)
    sessions: int = 0

    @property
    def final_equity(self) -> Decimal:
        return self.equity_curve[-1].value if self.equity_curve else Decimal("0")


@dataclass(slots=True)
class _Position:
    """Open exposure, and what it cost to acquire."""

    quantity: int = 0
    cost_basis: Decimal = Decimal("0")
    opened_at: date | None = None

    @property
    def is_open(self) -> bool:
        return self.quantity > 0


def run(
    strategy: Strategy,
    data: MarketData,
    *,
    instrument_id: int,
    calendar: MarketCalendar,
    start: date,
    end: date,
    interval: Interval = Interval.DAY_1,
    starting_cash: Decimal = Decimal("10000000"),
    costs: CostModel | None = None,
    execution_model: ExecutionModel = ExecutionModel.NEXT_OPEN,
    bar_minutes: int | None = None,
) -> BacktestResult:
    """Walk the sessions in `[start, end]` and simulate the strategy over them.

    The loop is deliberately boring. Every session: mark the portfolio at the
    close, ask the strategy, derive the fill instant, check it, fill it.
    Interesting loops are how look-ahead gets in.
    """
    cost_model = costs if costs is not None else CostModel()
    sessions = calendar.sessions_between(start, end)

    cash = starting_cash
    position = _Position()
    curve: list[EquityPoint] = []
    fills: list[Fill] = []
    trades: list[ClosedTrade] = []
    unfilled: list[UnfilledOrder] = []
    abstained: list[date] = []

    for day in sessions:
        decision_at = calendar.session_close(day)
        view = data.at(decision_at)

        # Mark to market first, so the curve reflects the portfolio the
        # strategy is about to judge rather than the one it produces.
        close = _latest_close(view, instrument_id, interval, decision_at)
        if close is not None:
            cash_plus_stock = cash + close * position.quantity
            curve.append(EquityPoint(day=day, value=cash_plus_stock))

        signal = strategy.evaluate(view, instrument_id)

        if signal is Signal.ABSTAIN:
            # The day stays in the curve. Only the judgement is withheld.
            abstained.append(day)
            continue

        buying = signal is Signal.ENTER and not position.is_open
        selling = signal is Signal.EXIT and position.is_open
        if not (buying or selling):
            continue

        execution_at = earliest_execution(
            calendar, decision_at, model=execution_model, bar_minutes=bar_minutes
        )
        assert_executable(
            calendar,
            decision_at=decision_at,
            execution_at=execution_at,
            model=execution_model,
            bar_minutes=bar_minutes,
        )

        quoted = data.at(execution_at).opening_price(instrument_id, interval)
        if quoted is None or quoted <= 0:
            unfilled.append(
                UnfilledOrder(
                    instrument_id=instrument_id,
                    decision_at=decision_at,
                    execution_at=execution_at,
                    buying=buying,
                    reason="no opening price at the execution instant",
                )
            )
            continue

        price = cost_model.fill_price(quoted, buying=buying)

        if buying:
            quantity = _affordable_quantity(cash, price, cost_model)
            if quantity <= 0:
                unfilled.append(
                    UnfilledOrder(
                        instrument_id=instrument_id,
                        decision_at=decision_at,
                        execution_at=execution_at,
                        buying=True,
                        reason="cash does not cover one share plus commission",
                    )
                )
                continue
        else:
            quantity = position.quantity

        commission = cost_model.commission(price * quantity)
        fill = Fill(
            instrument_id=instrument_id,
            decision_at=decision_at,
            execution_at=execution_at,
            buying=buying,
            quantity=quantity,
            price=price,
            commission=commission,
        )
        fills.append(fill)
        cash += fill.cash_delta

        if buying:
            position = _Position(
                quantity=quantity,
                cost_basis=price * quantity + commission,
                opened_at=execution_at.date(),
            )
        else:
            proceeds = price * quantity - commission
            trades.append(
                ClosedTrade(
                    instrument_id=instrument_id,
                    entry_at=position.opened_at or execution_at.date(),
                    exit_at=execution_at.date(),
                    pnl=proceeds - position.cost_basis,
                )
            )
            position = _Position()

    return BacktestResult(
        equity_curve=curve,
        fills=fills,
        trades=trades,
        unfilled=unfilled,
        abstained_sessions=abstained,
        sessions=len(sessions),
    )


def _latest_close(
    data: MarketData, instrument_id: int, interval: Interval, asof: datetime
) -> Decimal | None:
    """The most recent completed close, for marking the portfolio.

    Reading it through `bars` rather than by timestamp is deliberate: on a day
    the instrument did not trade, the portfolio is still worth something, and
    the honest value is the last price the market actually printed.
    """
    recent = data.bars(instrument_id, interval, limit=1)
    if not recent:
        return None
    bar = recent[-1]
    if bar.available_at > asof:
        # The reader already bounds this. Checking anyway costs one comparison
        # and turns a substituted or mis-wired data source into a failure
        # rather than into a better-looking equity curve.
        raise ValueError(
            f"market data returned a bar completing {bar.available_at.isoformat()}, "
            f"after the simulation instant {asof.isoformat()}"
        )
    return bar.close


def _affordable_quantity(cash: Decimal, price: Decimal, costs: CostModel) -> int:
    """Whole shares the cash covers, commission included.

    Whole shares because that is what these markets trade. Rounding up, or
    allowing fractions, would let the simulation deploy capital the account
    never had — a small, compounding overstatement that no single number would
    reveal.
    """
    if price <= 0:
        return 0
    # Solve for q in q*price + max(q*price*rate, floor) <= cash, by trying the
    # unconstrained answer and stepping down while it does not fit.
    rate = costs.commission_bps * BPS
    estimate = (cash / (price * (Decimal("1") + rate))).to_integral_value(rounding=ROUND_DOWN)
    quantity = int(estimate)
    while quantity > 0 and price * quantity + costs.commission(price * quantity) > cash:
        quantity -= 1
    return quantity
