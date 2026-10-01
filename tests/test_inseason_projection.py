from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from inseason_support import fixture
from pydantic import ValidationError

from fba.contracts.base import ConfigError
from fba.contracts.inseason import AdjustmentEntry, AdjustmentLedger, InseasonParameters
from fba.core.inseason import validate_inseason, validate_ledger
from fba.data.codec import canonical, digest
from fba.formulas.registry import evaluate, registry
from fba.inseason.adjustments import active_entries
from fba.inseason.projection import effective_projection


def test_parameter_validation_and_provenance():
    league, params, *_ = fixture()
    validate_inseason(league, params)
    broken = params.model_copy(update={"rate_k": {}})
    with pytest.raises(ConfigError, match="rate_k"):
        validate_inseason(league, broken)
    values = params.model_dump(mode="json")
    del values["minute_k"]["evidence"]
    with pytest.raises(ValidationError, match="evidence"):
        InseasonParameters.model_validate_json(__import__("json").dumps(values))


def test_every_registry_example_is_executable_and_single_owner():
    definitions = registry()
    assert len({f.id for f in definitions}) == len(definitions)
    assert len({f.implementation for f in definitions}) == len(definitions)
    for definition in definitions:
        trace = evaluate(definition.id, **definition.example)
        assert trace.result == definition.implementation(definition.example)
        assert definition.latex and definition.units
    assert evaluate(
        "blend", k=300.0, prior=0.52, total=412.0, sample=780.0
    ).result == pytest.approx(568 / 1080)


def test_flag_suggestions_and_sampling_error_use_recorded_formulas():
    from fba.inseason.projection import player_flags

    _, params, players, *_ = fixture()
    player = players.players[0]
    boxes = tuple(b for b in players.boxes if b.player_id == player.id)
    flags = player_flags(player, boxes, 60.0, {"FGA": 0.5}, (), params)
    role = next(f for f in flags if f.kind == "role")
    assert role.suggestions["minutes"].result == role.observed == 30.0
    for flag in flags:
        for trace in (*flag.traces, *flag.suggestions.values()):
            assert evaluate(trace.formula_id, **trace.inputs) == trace
    zero = player_flags(player, boxes, 30.0, {"FGA": 0.0}, (), params)
    assert all(not f.suggestions for f in zero if f.kind == "production")
    one = params.model_copy(
        update={"role_window": params.role_window.model_copy(update={"value": 1})}
    )
    flags = player_flags(player, boxes[:1], 60.0, {"FGA": 0.5}, (), one)
    assert [f.kind for f in flags] == ["role"]


def project(inputs, ledger=None, now=None, on=None):
    league, params, players, priors, original, _, at = inputs
    return effective_projection(
        players, priors, ledger or original, league, params, now or at, on or at.date()
    )


def test_empty_history_and_zero_shot_attempts_preserve_prior():
    args = list(fixture())
    args[2] = args[2].model_copy(update={"boxes": ()})
    result = project(args)
    assert result.players[0].minutes == 30.0
    assert result.players[0].probabilities == {"FG%": 0.5, "FT%": 0.75}
    assert all(v == 0 for v in result.players[0].weights.values())


def test_future_data_cannot_change_any_predecision_projection():
    args = list(fixture())
    baseline = project(args)
    box = (
        args[2]
        .boxes[0]
        .model_copy(
            update={
                "known_at": args[-1] + timedelta(days=1),
                "played_at": args[-1] + timedelta(days=1),
                "game_id": "future-secret",
                "minutes": 60.0,
            }
        )
    )
    player = (
        args[2]
        .players[0]
        .model_copy(update={"known_at": args[-1] + timedelta(days=1), "team_id": "SECRET"})
    )
    args[2] = args[2].model_copy(
        update={"boxes": (*args[2].boxes, box), "players": (*args[2].players, player)}
    )
    assert canonical(project(args)) == canonical(baseline)


def entry(inputs, value=36.0):
    at = inputs[-1]
    return AdjustmentEntry(
        id="a",
        group_id="g",
        player_id="p0",
        team_id="NBA0",
        field="minutes",
        value=value,
        starts_on=at.date(),
        ends_on=at.date() + timedelta(days=3),
        reason="new role",
        created_at=at,
        replaces=None,
        revokes=(),
    )


@given(st.floats(min_value=0, max_value=60, allow_nan=False, allow_infinity=False))
def test_append_only_undo_restores_exact_output_hash(minutes):
    inputs = fixture()
    original = project(inputs)
    change = entry(inputs, minutes)
    ledger = AdjustmentLedger(format_version=1, entries=(change,))
    assert project(inputs, ledger).players[0].minutes == minutes
    revoke = change.model_copy(update={"id": "undo", "group_id": "undo-group", "revokes": ("a",)})
    undone = AdjustmentLedger(format_version=1, entries=(change, revoke))
    validate_ledger(undone, inputs[1])
    assert digest(canonical(project(inputs, undone))) == digest(canonical(original))


def test_expiry_and_known_time_replay_and_group_revoke():
    inputs = fixture()
    change = entry(inputs)
    ledger = AdjustmentLedger(format_version=1, entries=(change,))
    assert active_entries(ledger, change.starts_on, change.created_at - timedelta(seconds=1)) == ()
    assert active_entries(ledger, change.ends_on + timedelta(days=1), change.created_at) == ()
    assert active_entries(ledger, change.starts_on, change.created_at) == (change,)


def test_new_multiplier_field_needs_only_configuration():
    inputs = list(fixture())
    field = (
        inputs[1]
        .fields[1]
        .model_copy(update={"id": "test-rebound-scale", "targets": ("rate:REB",)})
    )
    inputs[1] = inputs[1].model_copy(update={"fields": (*inputs[1].fields, field)})
    change = entry(inputs, 2.0).model_copy(update={"field": field.id})
    before = project(inputs).players[0]
    after = project(inputs, AdjustmentLedger(format_version=1, entries=(change,))).players[0]
    assert after.expected["REB"] == before.expected["REB"] * 2


def test_player_without_prior_uses_configured_peer_group():
    inputs = list(fixture())
    inputs[3] = inputs[3].model_copy(update={"players": inputs[3].players[1:]})
    player = project(inputs).players[0]
    assert player.prior.player_id == "p0" and player.minutes > 0


def test_undo_of_replacement_restores_original_adjustment():
    inputs = fixture()
    original = entry(inputs, 35.0)
    replaced = original.model_copy(update={"id": "b", "replaces": "a", "value": 20.0})
    undo = original.model_copy(update={"id": "undo-b", "revokes": ("b",)})
    ledger = AdjustmentLedger(format_version=1, entries=(original, replaced, undo))
    assert active_entries(ledger, inputs[-1].date(), inputs[-1]) == (original,)


def test_registered_formulas_have_hand_calculated_examples():
    expected = {
        "effective_samples": 1.6,  # (1 + 3)^2 / (1^2 + 3^2)
        "monitor_margin": 0.098,
        "exposure_rate": 1.0,
        "exposure_error": 0.316228,
        "weighted_mean": 2.5,
        "complementarity": 0.125,
        "path_bid": 6,
        "historical_share": 8,
        "empirical_error": 1,
        "rank_correlation": -1,
        "median": 2.5,
        "market_normalization": 0,
        "healthy_capacity": 50,
        "clipped_affine": 7,
        "budget_fraction": 0.8,
        "mean": 3,
        "variance": 1,
        "standardize": 2,
        "ratio_impact": 1,
        "season_utility": 1,
        "dollar_scale": 28,
        "dollar_value": 57,
        "bidder_wealth": 1.5,
        "anchor": 15,
        "inflation": 1.2,
        "sale_cost": 2.2,
        "winning_cost": 3,
        "planning_cost": 6,
        "affordable_cap": 25,
        "blend": 0.525926,
        "minutes": 30.136,
        "weight": 0.25,
        "expectation": 12.0,
        "calibration": 0.82,
        "score_calibration": 5.7,
        "z": 1.0,
        "normal": 0.5,
        "error": 0.05,
        "probability": 0.65,
        "add_score": 0.3,
        "difference": 0.2,
        "rank_value": 10.0,
        "trade_value_ratio": 0.7,  # min(100, 70) / max(100, 70)
        "acceptance": 0.5,
        "product": 0.2,
        "brier": 0.04,
        "log_loss": 0.693147,
        "mae": 1.0,
        "reallocate": 27.0,
        "linear": 8.0,
        "ratio": 0.75,
        "threshold": 1.0,
    }
    assert set(expected) == {f.id for f in registry()}
    for definition in registry():
        assert set(definition.input_units) == set(definition.example)
        assert evaluate(definition.id, **definition.example).result == pytest.approx(
            expected[definition.id], abs=0.0005
        )


def test_k_requirements_are_derived_from_scored_categories():
    inputs = list(fixture())
    league, params = inputs[:2]
    assert "OREB" not in {c.id for c in league.categories}
    inputs[1] = params.model_copy(
        update={"rate_k": {s: p for s, p in params.rate_k.items() if s != "OREB"}}
    )
    validate_inseason(league, inputs[1])
    assert "OREB" not in project(inputs).players[0].rates
    missing_scored = inputs[1].model_copy(
        update={"rate_k": {s: p for s, p in inputs[1].rate_k.items() if s != "AST"}}
    )
    with pytest.raises(ConfigError, match="AST"):
        validate_inseason(league, missing_scored)


def test_shooting_prior_with_no_attempts_uses_position_peer_probability():
    inputs = list(fixture())
    priors = inputs[3]
    without_probability = priors.players[0].model_copy(update={"probabilities": {"FG%": 0.5}})
    inputs[3] = priors.model_copy(update={"players": (without_probability, *priors.players[1:])})
    assert project(inputs).players[0].probabilities["FT%"] == pytest.approx(0.75)
