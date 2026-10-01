import ast
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from inseason_support import simulation

from fba.contracts.base import DataError
from fba.contracts.config import Term
from fba.formulas.arrays import ARRAY_FORMULAS, evaluate_array
from fba.formulas.categories import total_terms
from fba.formulas.registry import definitions, evaluate, registry
from fba.inseason.today import today
from fba.inseason.trades import complementary_teams, evaluate_trade


@pytest.mark.parametrize("shape", ((2,), (3, 2), (2, 3, 2)))
@pytest.mark.parametrize("coefficient", (-2.0, 0.0, 1.0))
def test_single_term_totals_preserve_shape_zero_sign_and_input_independence(shape, coefficient):
    box = np.resize(np.array([9.0, 2.0, 8.0, -1.0, 7.0, -0.0]), shape)
    saved = box.copy()
    expected = np.zeros(shape[:-1])
    expected = expected + box[..., 1] * coefficient
    actual = total_terms(box, (Term(stat_id="value", coefficient=coefficient),), ("other", "value"))
    assert isinstance(actual, np.ndarray) and actual.shape == shape[:-1]
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(np.signbit(actual), np.signbit(expected))
    actual[...] = 999.0
    np.testing.assert_array_equal(box, saved)


def test_registered_scalar_implementations_are_one_to_one_and_complete():
    rows = registry()
    assert len({r.id for r in rows}) == len(rows)
    assert len({r.implementation for r in rows}) == len(rows)
    source = Path(__file__).parents[1] / "src/fba/formulas/scalar.py"
    functions = {
        node.name
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.FunctionDef)
    }
    # These functions dispatch, validate inputs or expose metadata; they are not equations.
    helpers = {"number", "vector"}
    assert {r.implementation.__name__ for r in rows} == functions - helpers


def simulation_example_answers():
    return {
        "health_step": [1, 1, 0],
        "subset_bound": [1, 2],
        "outward_bound": [1, -2],
        "sample_mean": [2, 5],
        "sample_variance": [1, 4],
        "sample_deviation": [1, 2],
        "linear_totals": [1, 9],
        "category_ratio": [3, 3],
        "game_threshold": [1, 0],
        "comparison_margin": [0, 2],
        "linear_interval": [[-2], [4]],
        "ratio_interval": [[0.5], [3]],
        "standings_credit": [4, 4.5],
        "playoff_odds": [0.5, 0.5],
        "lognormal_taste": [1, np.e],
        "health_decay": [[1], [0.5], [0.25]],
        "control_covariance": [[1]],
        "normal_quantile": [0, 1],
        "scheduled_health": [1, 0.5],
        "ranked_health_value": [4, -2],
        "portfolio_cost_floor": [4],
        "highest_bid_survival": [0.75, 0],
        "median_bid": [4],
    }


def vector_example_answers():
    return {
        **simulation_example_answers(),
        "availability_regression": [1, 3],
        "row_rates": [[0.5, 0.2], [0.6, 0.2]],
        "matrix_product": [11],
        "bernoulli_mean": [[1, 2]],
        "bernoulli_covariance": [[[2, 2], [2, 5]]],
        "availability_value": [4, 16],
        "sample_covariance": [[2, 2], [2, 2]],
        "control_variate": [4, 5],
        "stratified_uniform": [[0.25, 0], [0.75, 0.5]],
        "standard_deviation_floor": [0.75, 1],
        "central_difference": [1, 1.5],
        "utility_rescale": [2, 4],
        "covariance_root": [[2, 0], [0, 3]],
        "average_ranks": [2, 0.5, 0.5],
        "soft_majority": [0.5, 0.5],
        "category_marginals": [[0, 1], [0, 1]],
        "bootstrap_scale": [[6, 3]],
        "sampling_covariance": [[3, 1], [1, 3]],
        "standardized_margins": [[1, 2]],
        "logistic_objective": np.log(2),
        "logistic_gradient": [-0.25],
        "logistic_hessian": [[0.625]],
        "calibration_fit": 0.5,
        "health_transitions": [[0.5, 0.5], [0.5, 0.125]],
        "conditional_health": [0.75, 0.85],
        "health_count_covariance": [[3, 6], [6, 12]],
        "managed_covariance": [[[[5]]]],
        "utility_blend": [2.5, 3],
        "bid_distribution": [[0, 1, 1], [0, 0, 1]],
        "second_bid_survival": [0.75, 0],
        "bounded_mean": [2, 2],
        "weighted_rows": [2, 3],
        "weighted_second": [[5, 7], [7, 10]],
        "centered_covariance": [[1, 1], [1, 1]],
        "category_points": [[1, 0.5, 0], [0, 1, 0.5]],
        "week_points": [0.5, 1],
        "calibrate_distribution": [[1, 2, 3], [2, 4, 6]],
        "rounding_distribution": [[[1, 2, 3, 0.5], [0, 1, 1, 0], [0, 1, 1, 0.5]]],
    }


def test_vector_examples_have_independent_answers_and_immutable_replay_inputs():
    expected = vector_example_answers()
    assert len({row.id for row in definitions()}) == len(definitions())
    assert set(expected) == {row.id for row in ARRAY_FORMULAS}
    source = Path(__file__).parents[1] / "src/fba/formulas/vector.py"
    functions = {
        node.name
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.FunctionDef)
    }
    # Schema validation is not an equation.
    assert {
        r.implementation.__name__
        for r in ARRAY_FORMULAS
        if r.implementation.__module__ == "fba.formulas.vector"
    } == functions - {"scoring_axes"}
    simulation_source = source.with_name("simulation.py")
    simulation_functions = {
        node.name
        for node in ast.parse(simulation_source.read_text()).body
        if isinstance(node, ast.FunctionDef)
    }
    assert {
        r.implementation.__name__
        for r in ARRAY_FORMULAS
        if r.implementation.__module__ == "fba.formulas.simulation"
    } == simulation_functions - {
        "reduction_axes",
        "mean_array",
        "variance_array",
        "deviation_array",
        "subset_interval",
        "outward_interval",
    }
    for row in ARRAY_FORMULAS:
        inputs = {k: np.asarray(v, dtype=float) for k, v in row.example.items()}
        actual = evaluate_array(row.id, **inputs)
        np.testing.assert_allclose(actual.result, expected[row.id], atol=1e-12)
        trace = actual.trace
        for value in inputs.values():
            value[...] = 999
        assert actual.trace == trace
        replay = evaluate_array(row.id, **{k: np.asarray(v) for k, v in trace.inputs.items()})
        np.testing.assert_array_equal(actual.result, replay.result)


@pytest.mark.parametrize(
    "inputs",
    [
        {"covariance": np.ones((2, 3))},
        {"covariance": np.array([[float("nan")]])},
        {"unknown": np.ones((2, 2))},
    ],
)
def test_array_formula_rejects_invalid_evidence(inputs):
    with pytest.raises(DataError, match="formula.covariance_root"):
        evaluate_array("covariance_root", **inputs)


@pytest.mark.parametrize(
    "projected,observed",
    [([1], [2]), ([1, 2], [[2, 3]]), ([1, 1], [2, 3]), ([1, 2], [2, 3.5]), ([1, 2], [-1, 3])],
)
def test_availability_formula_rejects_unpaired_or_invalid_game_counts(projected, observed):
    with pytest.raises(DataError):
        evaluate_array("availability_regression", projected=projected, observed=observed)


def test_scalar_shaped_array_calculation_is_immutable_and_replayable():
    result = evaluate_array("central_difference", plus=3.0, minus=1.0, step=2.0)
    assert result.result.shape == ()
    assert result.trace.result == 0.5
    assert not result.result.flags.writeable
    assert evaluate_array(result.formula_id, **result.trace.inputs).trace == result.trace


def test_distribution_trace_replays_empty_pairs_and_rejects_fractional_axes():
    definition = next(row for row in ARRAY_FORMULAS if row.id == "calibrate_distribution")
    inputs = {k: np.asarray(v, dtype=float) for k, v in definition.example.items()}
    inputs["nested"] = np.empty((0, 2))
    original = evaluate_array(definition.id, **inputs)
    replay = evaluate_array(
        definition.id, **{k: np.asarray(v) for k, v in original.trace.inputs.items()}
    )
    np.testing.assert_array_equal(replay.result, original.result)
    with pytest.raises(DataError, match="integer axis"):
        evaluate_array(definition.id, **{**inputs, "scoring_axis": 1.5})


def walk_traces(value):
    if isinstance(value, dict):
        if "formula_id" in value:
            yield value
        else:
            for child in value.values():
                yield from walk_traces(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from walk_traces(child)


def test_recorded_matchup_trade_and_lineup_inputs_reproduce_displayed_numbers():
    sim = simulation()
    week = sim.week("team0", "team1", "2")
    trade = evaluate_trade(sim, "team0", "team1", ("p0",), ("p3",))
    daily = today(sim, sim.as_of.astimezone(sim.zone).date(), "Asia/Taipei")
    traces = list(walk_traces([x.model_dump() for x in (week, trade, daily)]))
    assert len(traces) >= 20
    for trace in traces:
        assert evaluate(trace["formula_id"], **trace["inputs"]).result == pytest.approx(
            trace["result"], abs=1e-12
        )
    for row in daily.players:
        if row.marginal is not None:
            assert row.marginal.result >= -sim.params.tolerance.value
            assert row.opponents and row.tipoffs
            assert row.category_changes


def test_complementary_partner_and_category_changes_replay_from_recorded_inputs():
    sim = simulation()
    partners = complementary_teams(sim)
    assert len(partners) == len(sim.snapshot.teams) - 1
    for partner in partners:
        assert partner.score == partner.traces[0].result
        for trace in partner.traces:
            assert evaluate(trace.formula_id, **trace.inputs) == trace
    trade = evaluate_trade(sim, "team0", "team1", ("p0",), ("p3",))
    for before, after, changes in (
        (trade.before, trade.after, trade.category_changes),
        (trade.opponent_before, trade.opponent_after, trade.opponent_category_changes),
    ):
        original = {(w.week_id, c.id): c.probability for w in before for c in w.categories}
        for week in after:
            for category in week.categories:
                trace = changes[week.week_id][category.id]
                assert trace.inputs == {
                    "before": original[week.week_id, category.id],
                    "after": category.probability,
                }
                assert evaluate(trace.formula_id, **trace.inputs) == trace


def test_both_apps_use_identical_registry_formula_markup():
    root = Path(__file__).parents[1]
    js = (root / "src/fba/runtime/static/formulas.js").read_text()
    # Deterministic DOM serialization, with no browser or network dependencies.
    harness = """
const fs = await import("node:fs");
const rows = JSON.parse(fs.readFileSync(0, "utf8"));
class Element {
  constructor(tag) {this.tag = tag; this.children = []; this.textContent = ""; this.className = "";}
  append(...nodes) {this.children.push(...nodes);}
}
globalThis.document = {createElement: tag => new Element(tag)};
console.log(JSON.stringify(rows.map(row => formula(row.example, rows))));
"""
    rows = [f.model_dump(mode="json") for f in definitions()]
    result = subprocess.run(
        ["node", "--input-type=module", "-e", js + harness],
        input=json.dumps(rows),
        text=True,
        capture_output=True,
        check=True,
    )
    rendered = json.loads(result.stdout)
    assert len(rendered) == len(rows)
    for row, markup in zip(rows, rendered, strict=True):
        assert markup["children"][1] == {
            "tag": "code",
            "children": [],
            "textContent": row["latex"],
            "className": "",
        }
        table_rows = markup["children"][2]["children"][1]["children"]
        assert [cells["children"][2]["textContent"] for cells in table_rows] == list(
            row["input_units"].values()
        )
    assert 'from "/formulas.js"' in (root / "src/fba/apps/static/app.js").read_text()
    assert 'from "/formulas.js"' in (root / "src/fba/apps/inseason/static/forms.js").read_text()


@pytest.mark.parametrize(
    "formula,changed",
    (
        ("playoff_odds", {"places": 0.5}),
        ("playoff_odds", {"tolerance": 0.0}),
        ("normal_quantile", {"epsilon": 0.0}),
        ("normal_quantile", {"uniform": [-0.1, 0.5]}),
        ("portfolio_cost_floor", {"values": [1.0, 1.0]}),
        ("portfolio_cost_floor", {"slots": 2.0}),
        ("portfolio_cost_floor", {"multipliers": [-1.0]}),
        ("median_bid", {"increment": 0.0}),
    ),
)
def test_simulation_equations_reject_invalid_numeric_domains(formula, changed):
    row = next(r for r in ARRAY_FORMULAS if r.id == formula)
    inputs = {k: np.asarray(v, dtype=float) for k, v in {**row.example, **changed}.items()}
    with pytest.raises(DataError, match="formula." + formula):
        evaluate_array(formula, **inputs)


@pytest.mark.parametrize("dtype", (np.bool_, np.int32, np.float64))
def test_sample_mean_reuses_input_storage_for_boolean_integer_and_float_samples(dtype):
    from fba.formulas.simulation import mean_array

    class Samples(np.ndarray):
        def mean(self, *args, **kwargs):
            assert np.shares_memory(self, original)
            calls.append(True)
            return super().mean(*args, **kwargs)

    original = np.array([[0, 2], [2, 4]], dtype=dtype)
    values = original.view(Samples)
    calls = []
    actual = mean_array(values, axis=0)
    expected = [0.5, 1.0] if dtype == np.bool_ else [1.0, 3.0]
    np.testing.assert_array_equal(actual, expected)
    assert calls == [True]  # No full-array float conversion before reducing.


def test_health_equations_accept_boolean_evidence_without_changing_inputs():
    from fba.formulas.simulation import ranked_health_value, scheduled_health

    health = np.array([True, False])
    values = ranked_health_value(
        {
            "health": health,
            "healthy": np.array([2.0, 3.0]),
            "unhealthy": np.array([0.0, -1.0]),
            "value": np.array([2.0, 2.0]),
        }
    )
    games = scheduled_health(
        {
            "scheduled": np.array([[True, False], [True, True]]),
            "probabilities": np.full((2, 2), 0.5),
        }
    )
    np.testing.assert_array_equal(values, [4.0, -2.0])
    np.testing.assert_array_equal(games, [1.0, 0.5])
    np.testing.assert_array_equal(health, [True, False])
