import json
from pathlib import Path

import numpy as np
import pytest

from fba.contracts.base import DataError
from fba.contracts.config import CalculationModel, PriorWeight, SeasonConfig
from fba.contracts.projection import Prior, ProjectionPlayer
from fba.core.distribution import moments
from fba.core.projection import prior


@pytest.fixture
def model():
    return CalculationModel.model_validate_json(
        (Path(__file__).parents[1] / "examples/2026-27/model.json").read_bytes()
    )


def test_distribution_has_independent_closed_form_threshold_moments(model):
    season = SeasonConfig.model_validate_json(
        (Path(__file__).parents[1] / "examples/2026-27/season.json").read_bytes()
    )
    threshold = next(s.definition for s in season.stat_definitions if s.id == "DD")
    # Points always qualify. Independent REB/AST rounds cross 10 with probability 1/2.
    # DD = their union: P=3/4, variance=3/16; Cov(REB, DD)=Cov(AST, DD)=1/8.
    stats = (4.0, 8.0, 2.0, 3.0, 10.0, 0.0, 9.5, 2.0, 9.5, 1.0, 0.0, 0.0)
    player = ProjectionPlayer(
        id="x",
        name="x",
        team_id="a",
        priors=(),
        games_cap=None,
        return_on=None,
        history=(stats,),
    )
    result = moments(player, 60.0, 30.0, stats, model.projection, threshold, 8)
    assert result.stats == (*stats, 0.75)
    assert result.covariance[-1][-1] == 0.1875
    assert result.covariance[6][-1] == result.covariance[8][-1] == 0.125
    assert result.covariance[6][8] == 0
    assert result.covariance[6][6] == 0.25
    assert np.linalg.eigvalsh(result.covariance).min() >= -1e-8
    broken = list(stats)
    broken[0] = 9.0
    with pytest.raises(DataError, match="infeasible"):
        moments(player, 60.0, 30.0, tuple(broken), model.projection, threshold, 8)


def test_distribution_batching_preserves_known_answer(model):
    season = SeasonConfig.model_validate_json(
        (Path(__file__).parents[1] / "examples/2026-27/season.json").read_bytes()
    )
    threshold = next(s.definition for s in season.stat_definitions if s.id == "DD")
    row = (4.0, 8.0, 2.0, 3.0, 11.0, 1.0, 9.5, 2.0, 9.5, 1.0, 0.0, 0.0)
    player = ProjectionPlayer(
        id="x",
        name="x",
        team_id="a",
        priors=(),
        games_cap=None,
        return_on=None,
        history=(row,) * 257,
    )
    one = moments(player, 60.0, 30.0, row, model.projection, threshold, 8)
    settings = model.projection.model_copy(update={"integration_batch_size": 17})
    other = moments(player, 60.0, 30.0, row, settings, threshold, 8)
    assert one == other


def test_covariance_rounding_boundary_is_platform_independent(model):
    root = Path(__file__).parents[1]
    data = json.loads((root / "tests/fixtures/covariance-reference.json").read_text())
    season = SeasonConfig.model_validate_json((root / "examples/2026-27/season.json").read_bytes())
    threshold = next(s.definition for s in season.stat_definitions if s.id == "DD")
    player = ProjectionPlayer.model_validate_json(json.dumps(data["player"]))
    result = moments(
        player,
        data["games"],
        data["minutes"],
        tuple(data["target"]),
        model.projection,
        threshold,
        8,
    )
    i, j = data["covariance_indices"]
    assert result.covariance[i][j] == result.covariance[j][i] == data["expected_covariance"]
    assert result.covariance[i][j] == pytest.approx(data["independent_covariance"], abs=1.01e-8)


def test_priors_preserve_minutes_and_single_source(model):
    stats = (4.0, 8.0, 2.0, 3.0, 10.0, 0.0, 9.5, 2.0, 9.5, 1.0, 0.0, 0.0)
    player = ProjectionPlayer(
        id="x",
        name="x",
        team_id="a",
        priors=(
            Prior(id="a", expected_games=60.0, minutes=30.0, stats=stats),
            Prior(id="b", expected_games=70.0, minutes=40.0, stats=stats),
        ),
        games_cap=None,
        return_on=None,
        history=(stats,),
    )
    equal = model.projection.model_copy(
        update={"prior_weights": (PriorWeight(id="a", weight=0.5), PriorWeight(id="b", weight=0.5))}
    )
    assert prior(player, equal)[:2] == (65.0, 35.0)
    assert prior(player, model.projection)[:2] == (70.0, 40.0)
    solo = player.model_copy(update={"priors": player.priors[:1]})
    assert prior(solo, model.projection) == (60.0, 30.0, stats)
    unknown = player.model_copy(
        update={"priors": (player.priors[0].model_copy(update={"id": "unknown"}),)}
    )
    with pytest.raises(DataError, match="unconfigured"):
        prior(unknown, model.projection)


def test_gp_calibration_clips_availability_and_preserves_per_game_moments():
    from fba.contracts.data import Calibration
    from fba.contracts.projection import Projected
    from fba.core.projection import calibrate_availability

    fit = Calibration(
        training_season_id="previous",
        method="ordinary_least_squares",
        intercept=-2.0,
        slope=0.8,
        sample_size=10,
        inputs_sha256=("0" * 64,),
    )
    players = tuple(
        Projected(id=str(g), expected_games=g, minutes=20.0, stats=(10.0,), covariance=((2.0,),))
        for g in (0.0, 10.0, 100.0)
    )
    result = calibrate_availability(players, fit, 60, 8)
    assert tuple(p.expected_games for p in result) == (0.0, 6.0, 60.0)
    assert all(
        p.stats == q.stats and p.minutes == q.minutes and p.covariance == q.covariance
        for p, q in zip(players, result, strict=True)
    )
