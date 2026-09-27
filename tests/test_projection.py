import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize import minimize

from fba.contracts.base import DataError
from fba.contracts.config import CalculationModel, SeasonConfig
from fba.contracts.projection import Prior, ProjectionPlayer, ProjectionTeam
from fba.core.distribution import moments
from fba.core.projection import reconcile_team, weighted_cap


@pytest.fixture
def model():
    return CalculationModel.model_validate_json(
        (Path(__file__).parents[1] / "examples/2026-27/model.json").read_bytes()
    )


def independent_solution(target, scales, weights, limit):
    return minimize(
        lambda x: 0.5 * np.sum(((x - target) / scales) ** 2),
        target * 0.5,
        jac=lambda x: (x - target) / scales**2,
        bounds=[(0, x) for x in target],
        constraints=[
            {"type": "ineq", "fun": lambda x: limit - weights @ x, "jac": lambda x: -weights}
        ],
        method="SLSQP",
        options={"ftol": 1e-12, "maxiter": 1000},
    )


def test_allocation_matches_closed_form_and_independent_optimizer(model):
    settings = model.projection
    actual = weighted_cap((100.0, 100.0, 100.0), (1.0, 2.0, 3.0), (1.0, 1.0, 1.0), 240.0, settings)
    np.testing.assert_allclose(actual, 100 - 60 * np.array([1, 4, 9]) / 14, atol=1e-10)
    rng = np.random.default_rng(42)
    for _ in range(20):
        target = rng.uniform(5, 40, 8)
        scales = rng.uniform(1, 5, 8)
        weights = rng.uniform(0, 1, 8)
        limit = float(weights @ target) * 0.7
        result = weighted_cap(tuple(target), tuple(scales), tuple(weights), limit, settings)
        oracle = independent_solution(target, scales, weights, limit)
        assert oracle.success
        np.testing.assert_allclose(result, oracle.x, atol=2e-5)
        order = rng.permutation(len(target))
        shuffled = weighted_cap(
            tuple(target[order]), tuple(scales[order]), tuple(weights[order]), limit, settings
        )
        np.testing.assert_allclose(np.array(shuffled)[np.argsort(order)], result, atol=1e-12)


def test_allocation_rejects_invalid_input_and_preserves_zero_exposure(model):
    p = model.projection
    assert weighted_cap((30.0, 20.0), (1.0, 1.0), (0.0, 1.0), 0.0, p) == (30.0, 0.0)
    for target, scales, weights, limit in [
        ((1.0,), (0.0,), (1.0,), 0.0),
        ((1.0,), (1.0,), (1.0,), -1.0),
        ((float("nan"),), (1.0,), (1.0,), 0.0),
    ]:
        with pytest.raises(DataError):
            weighted_cap(target, scales, weights, limit, p)


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
        catalog=True,
        priors=(),
        games_cap=None,
        return_on=None,
        minutes_sd=1.0,
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
        catalog=True,
        priors=(),
        games_cap=None,
        return_on=None,
        minutes_sd=1.0,
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


def test_final_team_resources_obey_budget_and_reject_inconsistent_scaling(model):
    stats = (4.0, 8.0, 2.0, 3.0, 10.0, 0.0, 9.5, 2.0, 9.5, 1.0, 0.0, 0.0)
    player = ProjectionPlayer(
        id="x",
        name="x",
        team_id="a",
        catalog=True,
        priors=(Prior(id="source", expected_games=1.0, minutes=30.0, stats=stats),),
        games_cap=None,
        return_on=None,
        minutes_sd=1.0,
        history=(stats,),
    )
    team = ProjectionTeam(
        id="a", dates=(date(2026, 10, 20),), full_season_games=1, possession_budget=0.0
    )
    _, minutes, row = reconcile_team((player,), team, model.projection)[0]
    assert minutes <= 240
    assert row[1] + 0.44 * row[3] + row[9] <= row[7] + 1e-8
    assert row[8] <= row[0] + 1e-8
    # Even a caller bypassing configuration loading cannot publish a violated resource limit.
    broken = model.projection.model_copy(update={"offense_stats": ()})
    with pytest.raises(DataError, match="final resource postcondition"):
        reconcile_team((player,), team, broken)
