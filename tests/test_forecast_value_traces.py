import numpy as np
import pytest
from inseason_support import fixture, simulation
from test_inseason_projection import entry, project
from test_inseason_proposals import resolved_proposals
from test_inseason_service import selected_session

from fba.contracts.base import DataError
from fba.contracts.config import Category, Linear, Ratio, Term
from fba.contracts.inseason import AdjustmentLedger
from fba.formulas.arrays import evaluate_array
from fba.formulas.categories import category_evidence
from fba.formulas.registry import evaluate
from fba.inseason.operations import refit_acceptance
from fba.inseason.projection import adjusted_values
from fba.inseason.review import calibration_bins
from fba.inseason.session import json_value


def test_category_value_trace_clips_base_negatives_and_freezes_inputs():
    category = Category(
        id="value",
        label="Value",
        direction="higher",
        comparison_decimals=3,
        tie_value=0.5,
        formula=Linear(
            kind="linear", terms=tuple(Term(stat_id=s, coefficient=1) for s in ("a", "b", "c"))
        ),
    )
    box = np.array([[1e16, 1, -1e16], [4, 3, 2]], dtype=float)
    value, numerator, denominator, traces = category_evidence(box, category, ("a", "b", "c"))
    # Base-statistic negatives are clipped before category calculation.
    np.testing.assert_array_equal(value, [1e16, 9])
    np.testing.assert_array_equal(numerator, value)
    assert denominator is None
    saved = traces[0].inputs["values"]
    box[:] = 999
    assert traces[0].inputs["values"] == saved
    for trace in traces:
        replay = evaluate_array(
            trace.formula_id, **{k: np.asarray(v) for k, v in trace.inputs.items()}
        )
        assert replay.trace == trace


def test_category_value_trace_keeps_ordered_signed_cancellation():
    category = Category(
        id="value",
        label="Value",
        direction="higher",
        comparison_decimals=3,
        tie_value=0.5,
        formula=Linear(
            kind="linear",
            terms=(
                Term(stat_id="a", coefficient=1),
                Term(stat_id="b", coefficient=1),
                Term(stat_id="c", coefficient=-1),
            ),
        ),
    )
    value, _, _, traces = category_evidence(
        np.array([[1e16, 1, 1e16], [4, 3, 2]], dtype=float), category, ("a", "b", "c")
    )
    # Preserve the existing NumPy reduction order; fsum would return 1 for team one.
    np.testing.assert_array_equal(value, [0, 5])
    assert traces[-1].inputs["weights"] == (1, 1, -1)
    assert traces[-1].result == (0, 5)


@pytest.mark.parametrize("policy, expected", (("zero", 0), ("numerator", 2)))
def test_ratio_trace_keeps_total_ratio_and_zero_policy(policy, expected):
    category = Category(
        id="ratio",
        label="Ratio",
        direction="lower",
        comparison_decimals=3,
        tie_value=0.5,
        formula=Ratio(
            kind="ratio",
            numerator=(Term(stat_id="made", coefficient=1),),
            denominator=(Term(stat_id="attempt", coefficient=1),),
            zero_denominator=policy,
        ),
    )
    value, numerator, denominator, traces = category_evidence(
        np.array([[2.0, 10.0], [2.0, 0.0]]), category, ("made", "attempt")
    )
    np.testing.assert_array_equal(value, [0.2, expected])
    np.testing.assert_array_equal(numerator, [2, 2])
    np.testing.assert_array_equal(denominator, [10, 0])
    assert traces[-1].formula_id == "category_ratio"
    bad = category.model_copy(
        update={"formula": category.formula.model_copy(update={"zero_denominator": "error"})}
    )
    with pytest.raises(DataError, match="zero denominator"):
        category_evidence(np.array([[2.0, 0.0]]), bad, ("made", "attempt"))


def test_final_forecast_values_equal_trace_results_with_sample_independent_payload():
    sim = simulation()
    forecast = sim.week("team0", "team1", "2")
    for row in forecast.categories:
        assert row.value_axes == sim.axes
        assert row.value_traces[-1].result == (row.home, row.away)
        # Capture only two-team mean inputs, never the sample x day x player draws.
        source = row.value_traces[0].inputs["values"]
        assert len(source) == 2 and all(len(team) == len(sim.axes) for team in source)
        for trace in row.value_traces:
            replay = evaluate_array(
                trace.formula_id, **{k: np.asarray(v) for k, v in trace.inputs.items()}
            )
            assert replay.trace == trace
    legacy = forecast.categories[0].model_dump(exclude={"value_axes", "value_traces"})
    restored = type(forecast.categories[0]).model_validate(legacy)
    assert not restored.value_traces and not restored.value_axes


def test_calibration_bin_means_retain_actual_members_and_independent_answers():
    bins = calibration_bins((0.10, 0.15), (0, 1), 10)
    row = bins[1]
    assert (row.predicted, row.observed, row.difference) == (0.125, 0.5, 0.375)
    assert row.traces[0].inputs == {"values": (0.10, 0.15)}
    assert row.traces[1].inputs == {"values": (0, 1)}
    assert not bins[0].traces
    for trace in row.traces:
        assert evaluate(trace.formula_id, **trace.inputs) == trace


def test_adjustment_multiplier_retains_ordered_inputs_and_effective_projection_evidence():
    args = fixture()
    first = entry(args, 1.5).model_copy(update={"field": "usage"})
    second = first.model_copy(update={"id": "second", "value": 2.0})
    _, _, rates, _, traces = adjusted_values(
        30, 1, {"FGA": 10, "AST": 4}, {}, (first, second), args[1], False
    )
    assert rates == {"FGA": 30, "AST": 12}
    assert traces["adjustment:a:rate:FGA"].inputs == {"gain": 10, "probability": 1.5}
    assert traces["adjustment:second:rate:FGA"].inputs == {"gain": 15, "probability": 2}
    ledger = AdjustmentLedger(format_version=1, entries=(first,))
    player = project(args, ledger).players[0]
    for target in ("FGA", "FTA", "AST", "TO", "3PM"):
        trace = player.traces[f"adjustment:a:rate:{target}"]
        assert trace.result == player.rates[target]
        assert evaluate(trace.formula_id, **trace.inputs) == trace
    assert player.traces["final:FGM"].inputs["gain"] == player.rates["FGA"]


def test_acceptance_fit_report_retains_log_loss_and_bin_evidence(tmp_path):
    from math import log

    session = selected_session(tmp_path)
    features = [corner for corner in ((0, 0), (1, 0), (0, 1)) for _ in range(12)]
    outcomes = [outcome for _ in range(3) for outcome in ([True] * 6 + [False] * 6)]
    proposals = resolved_proposals(features, outcomes)
    for proposal in proposals:
        session.league_store().append_snapshot(
            "proposals",
            "synthetic test",
            proposal.created_at,
            json_value(proposal.model_dump(mode="json")),
        )
    report = refit_acceptance(session)
    assert isinstance(report, dict)
    assert report["log_loss"] == pytest.approx(log(2), abs=1e-12)
    assert report["traces"][0]["formula_id"] == "log_loss"
    assert report["traces"][0]["result"] == report["log_loss"]
    assert report["traces"][0]["inputs"]["predicted"] == [0.5] * 36
    assert report["traces"][0]["inputs"]["observed"] == [float(y) for y in outcomes]
    populated = [row for row in report["bins"] if row["count"]]
    assert len(populated) == 1 and populated[0]["predicted"] == populated[0]["observed"] == 0.5
    assert [trace["formula_id"] for trace in populated[0]["traces"]][:2] == ["mean", "mean"]
