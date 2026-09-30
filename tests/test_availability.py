import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_calculation import projection_bundle as projection_bundle
from test_team_minutes import budget_case as budget_case
from test_team_offense import offense_case as offense_case

from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import AvailabilityTail, ModelDocument
from fba.contracts.data import Calibration
from fba.core.config import validate_team_minutes
from fba.formulas.projection import calibrated_games
from fba.formulas.team_minutes import minute_allocations
from fba.projection.calculation import calculate


def calibration():
    return Calibration(
        training_season_id="2025-26",
        method="ordinary_least_squares",
        intercept=-25.9148,
        slope=1.18005,
        sample_size=374,
        inputs_sha256=("0" * 64,),
    )


def policy(anchor=40):
    model = json.loads((Path(__file__).parents[1] / "examples/2026-27/model.json").read_text())
    return AvailabilityTail.model_validate_json(
        json.dumps({"lower_anchor_games": anchor, "evidence": model["calibration"]["evidence"]})
    )


@pytest.mark.parametrize("games", [1, 4, 12, 18])
def test_positive_low_forecast_is_not_extrapolated_to_zero(games):
    assert calibrated_games(games, calibration(), 82, 8, policy()) > 0


def test_tail_is_monotone_continuous_and_zero_preserving():
    fit, tail = calibration(), policy()
    values = [calibrated_games(g / 10, fit, 82, 8, tail) for g in range(821)]
    assert values[0] == 0
    assert values == sorted(values)
    for games in (40, 55, 76, 82):
        assert calibrated_games(games, fit, 82, 8, tail) == calibrated_games(games, fit, 82, 8)
    assert calibrated_games(40 - 1e-6, fit, 82, 8, tail) == pytest.approx(values[400], abs=1e-6)
    assert calibrated_games(12, fit, 82, 8, policy(50)) != values[120]


def test_tail_requires_valid_explicit_settings_and_training_domain():
    for anchor in (0, -1, float("nan")):
        with pytest.raises(ValidationError):
            policy(anchor)
    for tail in (policy(100), policy(10)):
        with pytest.raises(DataError, match="availability_tail"):
            calibrated_games(12, calibration(), 82, 8, tail)


@pytest.mark.parametrize("games,expected", [(1, 0.5), (1.000000011, 0.5), (2.999999999, 1.5)])
def test_full_calculation_and_team_population_use_the_same_tail(offense_case, games, expected):
    i = offense_case
    data = i.config.model.model_dump(mode="json")
    data.update(format_version=11, availability_tail=policy(3).model_dump(mode="json"))
    model = ModelDocument.model_validate_json(json.dumps(data)).root
    p = i.players[0]
    p = p.model_copy(
        update={"priors": tuple(v.model_copy(update={"expected_games": games}) for v in p.priors)}
    )
    i = i.model_copy(
        update={
            "config": i.config.model_copy(update={"model": model}),
            "calibration": i.calibration.model_copy(update={"intercept": -1.5, "slope": 1.0}),
            "players": (p, *i.players[1:]),
        }
    )
    result = calculate(i, "0" * 64)
    projected = next(v for v in result.projections if v.id == p.id)
    allocated = next(
        r for a in minute_allocations(i) for r in a.allocations if r.catalog_id == p.id
    )
    assert projected.expected_games == projected.unconstrained_games == expected
    assert allocated.expected_games_before == allocated.expected_games_after == expected
    p = p.model_copy(update={"expected_games_override": 0.25})
    explicit = calculate(i.model_copy(update={"players": (p, *i.players[1:])}), "0" * 64)
    assert next(v for v in explicit.projections if v.id == p.id).expected_games == 0.25
    later = model.availability_tail.evidence.model_copy(
        update={"as_of": i.config.season.snapshot_as_of + timedelta(days=1)}
    )
    bad = model.model_copy(
        update={"availability_tail": model.availability_tail.model_copy(update={"evidence": later})}
    )
    with pytest.raises(ConfigError, match="availability_tail"):
        validate_team_minutes(bad, i.config.season, i.config.league)
    del data["availability_tail"]
    with pytest.raises(ValidationError):
        ModelDocument.model_validate_json(json.dumps(data))
