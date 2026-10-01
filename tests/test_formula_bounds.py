import numpy as np
import pytest

from fba.contracts.base import DataError
from fba.formulas.arrays import evaluate_array
from fba.formulas.registry import evaluate
from fba.formulas.scalar import upper_total
from fba.formulas.simulation import subset_interval


def test_conservative_aggregation_preserves_cancellation_and_one_ulp_margin():
    trace = evaluate("upper_total", values=(1e16, 1.0, -1e16))
    assert trace.result == float.fromhex("0x1.0000000000001p+0")
    assert evaluate(trace.formula_id, **trace.inputs) == trace
    bound = evaluate_array("outward_bound", value=np.array([1.0, -2.0]), direction=1.0)
    np.testing.assert_array_equal(
        bound.result,
        [float.fromhex("0x1.0000000000001p+0"), -float.fromhex("0x1.fffffffffffffp+0")],
    )


def test_optional_draw_encloses_every_subset_and_keeps_outward_signed_zero():
    lower, upper = subset_interval(
        {
            "lower": np.array([0.0, 5.0]),
            "upper": np.array([0.0, 5.0]),
            "draw": np.array([0.0, -3.0]),
            "fixed": np.asarray(0.0),
        }
    )
    assert lower[0] == -float.fromhex("0x0.0000000000001p-1022")
    assert upper[0] == float.fromhex("0x0.0000000000001p-1022")
    assert lower[1] < 2 <= 5 < upper[1]


def test_internal_infinite_bounds_remain_unavailable_for_public_trace():
    assert upper_total({"values": (float("inf"),)}) == float("inf")
    with pytest.warns(RuntimeWarning, match="overflow encountered"):
        with pytest.raises(DataError, match="non-finite result"):
            evaluate_array("outward_bound", value=np.finfo(np.float64).max, direction=1.0)


@pytest.mark.parametrize(
    "healthy,eligible,expected", [(0, 0, 0), (5, 0, 1), (2, 4, 0.5), (5, 4, 1)]
)
def test_availability_probability_has_explicit_empty_schedule_and_capacity_bounds(
    healthy, eligible, expected
):
    assert (
        evaluate("availability_probability", healthy_games=healthy, eligible_games=eligible).result
        == expected
    )


@pytest.mark.parametrize(
    "formula,inputs,expected",
    [
        ("positive_part", {"value": -3.0}, 0.0),
        ("positive_part", {"value": 4.0}, 4.0),
        ("absolute_error", {"predicted": 2.0, "observed": 5.0}, 3.0),
        ("absolute_error", {"predicted": 5.0, "observed": 2.0}, 3.0),
    ],
)
def test_error_and_positive_utility_have_independent_answers_and_replay(formula, inputs, expected):
    trace = evaluate(formula, **inputs)
    assert trace.result == expected
    assert evaluate(trace.formula_id, **trace.inputs) == trace
