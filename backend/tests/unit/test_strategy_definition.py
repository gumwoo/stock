"""A definition is what will be stored, so it has to stay what was run.

`frozen=True` freezes a dataclass's own fields, not the dict one of them
points at. A caller holding the original mapping could therefore change a
definition after it had already been used:

    params = {"short": 10, "long": 30}
    definition = StrategyDefinition(..., params=params)
    ...                                   # runs 10/30
    params["short"] = 20                  # now builds 20/30

Same object, same version, different behaviour — which is exactly what the
definition exists to make impossible, arriving through the one field that was
not actually frozen.

The other half is identity. A version is written by a person, and nothing
stops two definitions sharing one while behaving differently, so identity is
kind + version + params. `git_commit_sha` covers the remaining axis: a change
to the code behind the kind.
"""

from __future__ import annotations

import json

import pytest

from app.backtest import strategies
from app.backtest.strategies import (
    UnknownStrategyError,
    buy_and_hold,
    moving_average_cross,
)
from app.core.types import Interval, StrategyDefinition


def ma(**params: object) -> StrategyDefinition:
    return StrategyDefinition(kind="moving_average_cross", version="ma@v1", params=params)


class TestItCannotBeChangedAfterTheFact:
    def test_mutating_the_source_mapping_does_not_reach_it(self) -> None:
        params: dict[str, object] = {"short": 10, "long": 30}
        definition = StrategyDefinition(
            kind="moving_average_cross", version="ma-10-30@v1", params=params
        )

        params["short"] = 20

        assert definition.params["short"] == 10
        assert strategies.build(definition).short == 10  # type: ignore[union-attr]

    def test_the_stored_mapping_cannot_be_written_to(self) -> None:
        definition = moving_average_cross(short=10, long=30)

        with pytest.raises(TypeError):
            definition.params["short"] = 99  # type: ignore[index]

    def test_what_was_run_is_what_rebuilds(self) -> None:
        """The whole point, stated as the outcome."""
        params: dict[str, object] = {"short": 10, "long": 30}
        definition = ma(**params)
        first = strategies.build(definition)

        params["short"] = 20
        second = strategies.build(definition)

        assert first == second


class TestParametersMustSurviveStorage:
    @pytest.mark.parametrize(
        "value", [10, 1.5, "daily", True, None], ids=["int", "float", "str", "bool", "none"]
    )
    def test_json_safe_scalars_are_accepted(self, value: object) -> None:
        definition = StrategyDefinition(kind="buy_and_hold", version="v1", params={"x": value})

        assert definition.params["x"] == value

    @pytest.mark.parametrize("value", [[1, 2], {"a": 1}, object()], ids=["list", "dict", "object"])
    def test_anything_else_is_refused(self, value: object) -> None:
        """A row that cannot be read back is not a record of anything."""
        with pytest.raises(UnknownStrategyError, match="survive being stored"):
            StrategyDefinition(kind="buy_and_hold", version="v1", params={"x": value})

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_values_json_cannot_hold_are_refused(self, value: float) -> None:
        with pytest.raises(UnknownStrategyError, match="JSON cannot hold"):
            StrategyDefinition(kind="buy_and_hold", version="v1", params={"x": value})

    def test_an_enum_is_stored_as_its_value(self) -> None:
        """So it round-trips as a plain string and rebuilds from one."""
        definition = StrategyDefinition(
            kind="buy_and_hold", version="v1", params={"interval": Interval.DAY_1}
        )

        assert definition.params["interval"] == "1d"
        assert json.loads(definition.canonical)["params"]["interval"] == "1d"


class TestIdentity:
    def test_the_canonical_form_is_json(self) -> None:
        definition = moving_average_cross(short=10, long=30)

        assert json.loads(definition.canonical) == {
            "kind": "moving_average_cross",
            "version": "ma-10-30@v1",
            "params": {"short": 10, "long": 30},
        }

    def test_parameter_order_does_not_change_it(self) -> None:
        """Two definitions that run identically must compare identically."""
        first = StrategyDefinition(
            kind="moving_average_cross", version="v1", params={"short": 10, "long": 30}
        )
        second = StrategyDefinition(
            kind="moving_average_cross", version="v1", params={"long": 30, "short": 10}
        )

        assert first.canonical == second.canonical
        assert first.fingerprint == second.fingerprint

    def test_the_same_version_with_different_params_is_a_different_strategy(self) -> None:
        """Which is why a persisted run must not key on version alone."""
        first = ma(short=10, long=30)
        second = ma(short=20, long=60)

        assert first.version == second.version
        assert first.fingerprint != second.fingerprint

    def test_a_different_kind_is_a_different_strategy(self) -> None:
        assert buy_and_hold().fingerprint != moving_average_cross().fingerprint

    def test_the_fingerprint_is_stable_across_processes(self) -> None:
        """A digest of the canonical text, not of an object address."""
        import hashlib

        definition = moving_average_cross(short=10, long=30)
        expected = hashlib.sha256(definition.canonical.encode("utf-8")).hexdigest()[:16]

        assert definition.fingerprint == expected


class TestBuilding:
    def test_a_definition_rebuilds_the_strategy_it_describes(self) -> None:
        built = strategies.build(moving_average_cross(short=10, long=30))

        assert (built.short, built.long) == (10, 30)  # type: ignore[union-attr]

    def test_an_unknown_kind_raises(self) -> None:
        with pytest.raises(UnknownStrategyError, match="no strategy kind"):
            strategies.build(StrategyDefinition(kind="nope", version="v1"))

    def test_a_misspelled_parameter_raises_rather_than_defaulting(self) -> None:
        with pytest.raises(UnknownStrategyError, match="cannot build"):
            strategies.build(ma(shrot=10, long=30))

    def test_an_invalid_combination_raises_at_definition_time(self) -> None:
        """A helper catches it before a run starts, not partway through."""
        with pytest.raises(ValueError, match="must be under"):
            moving_average_cross(short=60, long=10)

    def test_a_kind_without_a_version_is_refused(self) -> None:
        with pytest.raises(UnknownStrategyError, match="needs a version"):
            StrategyDefinition(kind="buy_and_hold", version="  ")

    def test_describe_names_the_parameters(self) -> None:
        assert moving_average_cross(short=10, long=30).describe() == (
            "moving_average_cross@ma-10-30@v1 (long=30 short=10)"
        )
