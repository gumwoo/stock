"""The simulation loop, on a price sequence built by hand.

An artificial series, so every number below can be checked with a calculator.
The fake reader is deliberately strict: it refuses to return a bar that has not
completed, and it refuses an opening price for any instant other than the one
it is positioned at. If the engine ever reaches around the point-in-time rules,
these tests fail here rather than producing a slightly better equity curve.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.backtest.engine import (
    BPS,
    CostModel,
    MarketData,
    Signal,
    run,
)
from app.backtest.execution import ExecutionTimingError
from app.core.calendar import Market, MarketCalendar
from app.core.types import Bar, Interval

US = MarketCalendar(Market.US)
IID = 1

# Ten consecutive sessions, so every date below is a real trading day.
DAYS = US.sessions_between(date(2025, 11, 3), date(2025, 11, 14))

# The session after the window. Priced in every fixture on purpose: a backtest
# window is normally cut out of a longer history, so the data beyond `end`
# exists. A test whose fake simply lacks tomorrow proves nothing about whether
# the engine respects the window.
BEYOND = US.sessions_between(date(2025, 11, 17), date(2025, 11, 17))[0]


def _bar(day: date, price: str) -> Bar:
    value = Decimal(price)
    return Bar(
        ts=US.session_open(day),
        available_at=US.session_close(day),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1000"),
    )


class FakeMarket:
    """A reader that enforces the same boundaries `PitReader` does."""

    def __init__(self, prices: dict[date, str], *, asof: datetime | None = None) -> None:
        self._bars = {day: _bar(day, price) for day, price in prices.items()}
        self._asof = asof

    @property
    def asof(self) -> datetime:
        if self._asof is None:
            raise AssertionError("asked for data with no simulation instant")
        return self._asof

    def at(self, asof: datetime) -> MarketData:
        clone = type(self).__new__(type(self))  # subclasses keep their overrides
        clone._bars = self._bars
        clone._asof = asof
        return clone

    def bars(self, instrument_id: int, interval: Interval, *, limit: int = 250) -> list[Bar]:
        ready = [b for b in self._bars.values() if b.available_at <= self.asof]
        ready.sort(key=lambda b: b.ts)
        return ready[-limit:]

    def opening_price(self, instrument_id: int, interval: Interval) -> Decimal | None:
        for bar in self._bars.values():
            if bar.ts == self.asof:
                return bar.open
        return None


@dataclass
class ScriptedStrategy:
    """Emits a fixed sequence of signals, one per session."""

    script: list[Signal]
    seen: list[datetime] | None = None

    def __post_init__(self) -> None:
        self.seen = []
        self._index = 0

    def evaluate(self, data: MarketData, instrument_id: int) -> Signal:
        assert self.seen is not None
        self.seen.append(data.asof)
        signal = self.script[self._index] if self._index < len(self.script) else Signal.HOLD
        self._index += 1
        return signal


def flat_prices(value: str = "100") -> dict[date, str]:
    return dict.fromkeys([*DAYS, BEYOND], value)


def execute(
    script: list[Signal],
    prices: dict[date, str],
    *,
    costs: CostModel | None = None,
    cash: str = "10000",
):
    strategy = ScriptedStrategy(script=script)
    result = run(
        strategy,
        FakeMarket(prices),
        instrument_id=IID,
        calendar=US,
        start=DAYS[0],
        end=DAYS[-1],
        starting_cash=Decimal(cash),
        costs=costs if costs is not None else CostModel(Decimal("0"), Decimal("0")),
    )
    return result, strategy


class TestTheLoop:
    def test_it_walks_every_session_and_no_more(self) -> None:
        result, strategy = execute([Signal.HOLD] * len(DAYS), flat_prices())

        assert result.sessions == len(DAYS)
        assert strategy.seen is not None
        assert len(strategy.seen) == len(DAYS)

    def test_the_strategy_only_ever_sees_session_closes(self) -> None:
        _, strategy = execute([Signal.HOLD] * len(DAYS), flat_prices())

        assert strategy.seen is not None
        assert strategy.seen == [US.session_close(d) for d in DAYS]

    def test_doing_nothing_leaves_the_cash_untouched(self) -> None:
        result, _ = execute([Signal.HOLD] * len(DAYS), flat_prices())

        assert result.final_equity == Decimal("10000")
        assert result.fills == []


class TestFillsHappenAfterTheDecision:
    def test_the_entry_fills_at_the_next_session_open(self) -> None:
        result, _ = execute([Signal.ENTER], flat_prices())

        fill = result.fills[0]
        assert fill.decision_at == US.session_close(DAYS[0])
        assert fill.execution_at == US.session_open(DAYS[1])

    def test_no_fill_ever_lands_at_its_own_decision(self) -> None:
        script = [Signal.ENTER, Signal.EXIT, Signal.ENTER, Signal.EXIT]
        result, _ = execute(script, flat_prices())

        assert result.fills
        assert all(f.execution_at > f.decision_at for f in result.fills)

    def test_a_decision_on_the_last_session_cannot_fill(self) -> None:
        """Even though the price for the next session exists.

        This test used to pass for the wrong reason: the fake had no data past
        the window, so the fill failed on missing data rather than on the
        window. Against a real database — where a backtest period is cut out
        of a longer history — a decision on the final session filled on the
        session after it, outside the period the run claims to cover, and the
        equity curve never saw the position.
        """
        prices = flat_prices()
        assert prices[BEYOND], "the fixture must price the session after the window"

        script = [Signal.HOLD] * (len(DAYS) - 1) + [Signal.ENTER]
        result, _ = execute(script, prices)

        assert result.fills == []
        assert len(result.unfilled) == 1
        assert "outside the backtest window" in result.unfilled[0].reason

    def test_no_fill_lands_after_the_window_ends(self) -> None:
        script = [Signal.ENTER, Signal.EXIT] * len(DAYS)
        result, _ = execute(script, flat_prices())

        assert result.fills
        assert all(f.execution_at <= US.session_close(DAYS[-1]) for f in result.fills)

    def test_the_curve_and_the_fills_share_one_time_axis(self) -> None:
        """The symptom that made this visible: they disagreed."""
        script = [Signal.HOLD] * (len(DAYS) - 1) + [Signal.ENTER]
        result, _ = execute(script, flat_prices())

        last_day = result.equity_curve[-1].day
        assert all(f.execution_at.date() <= last_day for f in result.fills)

    def test_the_entry_price_is_the_next_open_not_the_decision_close(self) -> None:
        """The number that would differ if the engine read the wrong bar."""
        prices = flat_prices()
        prices[DAYS[0]] = "100"
        prices[DAYS[1]] = "120"
        result, _ = execute([Signal.ENTER], prices)

        assert result.fills[0].price == Decimal("120")


class TestCosts:
    def test_slippage_pushes_the_buy_up_and_the_sell_down(self) -> None:
        costs = CostModel(commission_bps=Decimal("0"), slippage_bps=Decimal("10"))
        result, _ = execute([Signal.ENTER, Signal.EXIT], flat_prices("100"), costs=costs)

        buy, sell = result.fills
        assert buy.price == Decimal("100") * (Decimal("1") + Decimal("10") * BPS)
        assert sell.price == Decimal("100") * (Decimal("1") - Decimal("10") * BPS)

    def test_a_round_trip_at_a_flat_price_loses_money(self) -> None:
        """Because it must. A costless round trip is a modelling claim."""
        costs = CostModel(commission_bps=Decimal("5"), slippage_bps=Decimal("5"))
        result, _ = execute([Signal.ENTER, Signal.EXIT], flat_prices("100"), costs=costs)

        assert result.trades[0].pnl < 0

    def test_commission_is_charged_on_both_legs(self) -> None:
        costs = CostModel(commission_bps=Decimal("10"), slippage_bps=Decimal("0"))
        result, _ = execute([Signal.ENTER, Signal.EXIT], flat_prices("100"), costs=costs)

        assert all(f.commission > 0 for f in result.fills)

    def test_zero_cost_is_expressible_and_exact(self) -> None:
        result, _ = execute([Signal.ENTER, Signal.EXIT], flat_prices("100"))

        assert result.trades[0].pnl == Decimal("0")


class TestSizing:
    def test_it_buys_whole_shares_only(self) -> None:
        result, _ = execute([Signal.ENTER], flat_prices("300"), cash="1000")

        assert result.fills[0].quantity == 3

    def test_it_never_spends_more_cash_than_it_has(self) -> None:
        costs = CostModel(commission_bps=Decimal("50"), slippage_bps=Decimal("0"))
        result, _ = execute([Signal.ENTER], flat_prices("300"), costs=costs, cash="1000")

        fill = result.fills[0]
        assert fill.price * fill.quantity + fill.commission <= Decimal("1000")

    def test_cash_that_cannot_cover_one_share_buys_nothing(self) -> None:
        result, _ = execute([Signal.ENTER], flat_prices("300"), cash="100")

        assert result.fills == []
        assert "cash does not cover" in result.unfilled[0].reason


class TestProfitAndLoss:
    def test_a_winning_trade(self) -> None:
        prices = flat_prices("100")
        prices[DAYS[1]] = "100"  # entry fill
        prices[DAYS[3]] = "150"  # exit fill
        result, _ = execute([Signal.ENTER, Signal.HOLD, Signal.EXIT], prices, cash="1000")

        # 10 shares bought at 100, sold at 150.
        assert result.fills[0].quantity == 10
        assert result.trades[0].pnl == Decimal("500")

    def test_the_curve_marks_the_position_at_each_close(self) -> None:
        prices = flat_prices("100")
        prices[DAYS[1]] = "100"
        prices[DAYS[2]] = "130"
        result, _ = execute([Signal.ENTER], prices, cash="1000")

        by_day = {p.day: p.value for p in result.equity_curve}
        # 10 shares at 130, no cash left over.
        assert by_day[DAYS[2]] == Decimal("1300")

    def test_the_curve_covers_every_session(self) -> None:
        result, _ = execute([Signal.HOLD] * len(DAYS), flat_prices())

        assert [p.day for p in result.equity_curve] == DAYS


class TestAbstain:
    def test_abstaining_opens_nothing(self) -> None:
        result, _ = execute([Signal.ABSTAIN] * len(DAYS), flat_prices())

        assert result.fills == []

    def test_the_day_stays_in_the_curve(self) -> None:
        """Deleting it would be a bias in itself."""
        result, _ = execute([Signal.ABSTAIN] * len(DAYS), flat_prices())

        assert [p.day for p in result.equity_curve] == DAYS
        assert result.abstained_sessions == DAYS

    def test_an_existing_position_is_left_alone(self) -> None:
        script = [Signal.ENTER] + [Signal.ABSTAIN] * (len(DAYS) - 1)
        result, _ = execute(script, flat_prices("100"), cash="1000")

        assert len(result.fills) == 1
        assert result.trades == []

    def test_it_is_not_the_same_as_hold(self) -> None:
        held, _ = execute([Signal.HOLD] * len(DAYS), flat_prices())
        abstained, _ = execute([Signal.ABSTAIN] * len(DAYS), flat_prices())

        assert held.abstained_sessions == []
        assert abstained.abstained_sessions == DAYS


class TestSessionsWithNoBar:
    """A session the calendar believes in and the market did not print.

    Live case: three sessions in Samsung's history where `exchange_calendars`
    says KRX traded and no bar exists. The engine marked them at the previous
    close — correct, the portfolio is worth something — and then asked the
    strategy anyway, handing it byte-identical inputs to the session before
    and counting the answer as a fresh judgement. It could have opened a
    position on the strength of re-reading yesterday.

    Valuing at a stale price is fine. Deciding on one is not.
    """

    @staticmethod
    def _with_a_hole() -> tuple[dict[date, str], date]:
        prices = flat_prices("100")
        gap = DAYS[4]
        del prices[gap]
        return prices, gap

    def test_the_strategy_is_not_asked(self) -> None:
        prices, gap = self._with_a_hole()
        _, strategy = execute([Signal.HOLD] * len(DAYS), prices)

        assert strategy.seen is not None
        assert gap not in [moment.date() for moment in strategy.seen]

    def test_every_other_session_is_still_asked(self) -> None:
        prices, _ = self._with_a_hole()
        _, strategy = execute([Signal.HOLD] * len(DAYS), prices)

        assert strategy.seen is not None
        assert len(strategy.seen) == len(DAYS) - 1

    def test_the_day_is_recorded(self) -> None:
        prices, gap = self._with_a_hole()
        result, _ = execute([Signal.HOLD] * len(DAYS), prices)

        assert result.sessions_without_data == [gap]
        assert not result.simulated_full_period

    def test_it_is_not_counted_as_an_abstention(self) -> None:
        """ABSTAIN is the strategy declining to judge. Here it was never asked,
        and summing the two would hide which happened."""
        prices, _ = self._with_a_hole()
        result, _ = execute([Signal.HOLD] * len(DAYS), prices)

        assert result.abstained_sessions == []

    def test_the_portfolio_is_still_marked_at_the_last_printed_price(self) -> None:
        prices, gap = self._with_a_hole()
        prices[DAYS[3]] = "150"
        result, _ = execute([Signal.HOLD] * len(DAYS), prices)

        by_day = {p.day: p.value for p in result.equity_curve}
        assert gap in by_day
        assert by_day[gap] == by_day[DAYS[3]]

    def test_the_curve_still_covers_every_session(self) -> None:
        prices, _ = self._with_a_hole()
        result, _ = execute([Signal.HOLD] * len(DAYS), prices)

        assert [p.day for p in result.equity_curve] == DAYS

    def test_a_stale_re_read_cannot_open_a_position(self) -> None:
        """The whole point, as an outcome rather than a mechanism.

        A strategy that would buy on exactly the empty session — and only
        then — buys nothing, because it is never consulted there.
        """
        prices, gap = self._with_a_hole()

        class EnterOnTheGap:
            def evaluate(self, data: MarketData, instrument_id: int) -> Signal:
                return Signal.ENTER if data.asof.date() == gap else Signal.HOLD

        result = run(
            EnterOnTheGap(),
            FakeMarket(prices),
            instrument_id=IID,
            calendar=US,
            start=DAYS[0],
            end=DAYS[-1],
            starting_cash=Decimal("10000"),
            costs=CostModel(Decimal("0"), Decimal("0")),
        )
        assert result.fills == []
        assert result.sessions_without_data == [gap]

    def test_a_decision_whose_fill_lands_on_the_hole_is_unfilled(self) -> None:
        """Different failure, same cause: there is no opening price to pay."""
        prices, gap = self._with_a_hole()
        before_gap = DAYS[3]

        class EnterTheDayBefore:
            def evaluate(self, data: MarketData, instrument_id: int) -> Signal:
                return Signal.ENTER if data.asof.date() == before_gap else Signal.HOLD

        result = run(
            EnterTheDayBefore(),
            FakeMarket(prices),
            instrument_id=IID,
            calendar=US,
            start=DAYS[0],
            end=DAYS[-1],
            starting_cash=Decimal("10000"),
            costs=CostModel(Decimal("0"), Decimal("0")),
        )
        assert result.fills == []
        assert "no opening price" in result.unfilled[0].reason
        assert result.unfilled[0].execution_at.date() == gap

    def test_the_same_strategy_does_trade_on_a_session_that_printed(self) -> None:
        """Otherwise the tests above pass on a strategy that never fires."""
        prices, _ = self._with_a_hole()
        traded = DAYS[1]  # its fill lands on DAYS[2], which printed

        class EnterOnThatDay:
            def evaluate(self, data: MarketData, instrument_id: int) -> Signal:
                return Signal.ENTER if data.asof.date() == traded else Signal.HOLD

        result = run(
            EnterOnThatDay(),
            FakeMarket(prices),
            instrument_id=IID,
            calendar=US,
            start=DAYS[0],
            end=DAYS[-1],
            starting_cash=Decimal("10000"),
            costs=CostModel(Decimal("0"), Decimal("0")),
        )
        assert len(result.fills) == 1


class TestDeterminism:
    def test_the_same_inputs_produce_the_same_run(self) -> None:
        script = [Signal.ENTER, Signal.HOLD, Signal.EXIT, Signal.ENTER]
        first, _ = execute(script, flat_prices("100"))
        second, _ = execute(script, flat_prices("100"))

        assert first.equity_curve == second.equity_curve
        assert first.fills == second.fills
        assert first.trades == second.trades


class TestItCannotReachAroundTheReader:
    def test_the_strategy_is_handed_a_positioned_reader(self) -> None:
        """Not the raw one, which would let it choose its own instant."""
        seen: list[datetime] = []

        class Peeking:
            def evaluate(self, data: MarketData, instrument_id: int) -> Signal:
                seen.append(data.asof)
                return Signal.HOLD

        run(
            Peeking(),
            FakeMarket(flat_prices()),
            instrument_id=IID,
            calendar=US,
            start=DAYS[0],
            end=DAYS[1],
        )
        assert seen == [US.session_close(DAYS[0]), US.session_close(DAYS[1])]

    def test_a_reader_that_leaks_a_future_bar_is_rejected(self) -> None:
        """The engine trusts the reader, and says so loudly if it should not."""

        class LeakyMarket(FakeMarket):
            def bars(
                self, instrument_id: int, interval: Interval, *, limit: int = 250
            ) -> list[Bar]:
                return [_bar(DAYS[-1], "999")]

        with pytest.raises(ValueError, match="after the simulation instant"):
            run(
                ScriptedStrategy(script=[Signal.HOLD]),
                LeakyMarket(flat_prices()),
                instrument_id=IID,
                calendar=US,
                start=DAYS[0],
                end=DAYS[0],
            )

    def test_an_end_before_the_start_is_refused(self) -> None:
        with pytest.raises(ValueError, match="precedes start"):
            run(
                ScriptedStrategy(script=[]),
                FakeMarket(flat_prices()),
                instrument_id=IID,
                calendar=US,
                start=DAYS[-1],
                end=DAYS[0],
            )


def test_the_timing_check_is_wired_in() -> None:
    """A sanity check that `assert_executable` is reachable from the engine.

    It should never fire in a correct run — the engine derives the instant it
    then checks — so this asserts the import is live rather than the behaviour,
    which `test_execution_clock` covers directly.
    """
    from app.backtest import engine

    assert engine.assert_executable is not None
    assert issubclass(ExecutionTimingError, Exception)
