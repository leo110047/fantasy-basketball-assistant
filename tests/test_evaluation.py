import json
from pathlib import Path

import numpy as np
import pytest
from scipy.stats import spearmanr

from fba.adapters.config import load_config
from fba.contracts.base import DataError
from fba.contracts.projection import EvaluationInput, PredictionVariant
from fba.core.evaluation import correlation, evaluate


@pytest.fixture
def frozen_evaluation():
    root = Path(__file__).parents[1]
    data = json.loads((root / "tests/fixtures/evaluation-reference.json").read_text())
    config = load_config(
        *(root / f"examples/2026-27/{n}.json" for n in ("league", "season", "model"))
    )
    league = config.league.model_copy(
        update={"categories": tuple(c for c in config.league.categories if c.id != "DD")}
    )
    payload = {
        "format_version": 1,
        "config": config.model_copy(update={"league": league}).model_dump(mode="json"),
        "artifacts": [],
        **{k: data[k] for k in ("stat_ids", "season_games", "actual", "predictions", "common_ids")},
    }
    return EvaluationInput.model_validate_json(json.dumps(payload)), data["expected"]


def test_reference_evaluator_reproduces_original_population_and_metrics(frozen_evaluation):
    inputs, expected = frozen_evaluation
    result = evaluate(inputs, "0" * 64)
    actual = result.variants[0]
    assert actual.predicted_players == 460
    assert actual.common_players == 324
    assert actual.rank_correlation == pytest.approx(expected["rank_correlation"], abs=1e-7)
    assert actual.top_draft_hits == expected["top_draft_hits"]
    assert actual.median_dollar_error == pytest.approx(expected["median_dollar_error"], abs=1e-7)
    assert actual.games_mae == pytest.approx(expected["games_mae"], abs=1e-7)
    variant = inputs.predictions[0].model_copy(
        update={"players": tuple(reversed(inputs.predictions[0].players))}
    )
    other = inputs.model_copy(
        update={"actual": tuple(reversed(inputs.actual)), "predictions": (variant,)}
    )
    assert evaluate(other, "0" * 64) == result


def test_correlation_matches_independent_tied_rank_oracle():
    rng = np.random.default_rng(42)
    for _ in range(30):
        x = tuple(float(v) for v in rng.integers(0, 7, 20))
        y = tuple(float(v) for v in rng.integers(0, 7, 20))
        assert correlation(x, y) == pytest.approx(spearmanr(x, y).statistic, abs=1e-12)
    with pytest.raises(DataError, match="constant"):
        correlation((1.0, 1.0), (1.0, 2.0))


def test_evaluation_does_not_silently_change_comparison_population(frozen_evaluation):
    inputs, _ = frozen_evaluation
    broken = inputs.model_copy(update={"common_ids": (*inputs.common_ids, "missing")})
    with pytest.raises(DataError, match="absent prediction"):
        evaluate(broken, "0" * 64)


def test_evaluation_rejects_malformed_prediction_axes(frozen_evaluation):
    inputs, _ = frozen_evaluation
    variant = inputs.predictions[0]
    broken_player = variant.players[0].model_copy(update={"stats": (1.0,)})
    broken_variant = variant.model_copy(update={"players": (broken_player, *variant.players[1:])})
    broken = inputs.model_copy(update={"predictions": (broken_variant,)})
    with pytest.raises(DataError, match="incompatible axes"):
        evaluate(broken, "0" * 64)
    with pytest.raises(DataError, match="absent from input axes"):
        evaluate(
            inputs.model_copy(update={"stat_ids": ("unknown", *inputs.stat_ids[1:])}), "0" * 64
        )


def test_selected_source_weights_improve_the_same_reference_population(frozen_evaluation):
    inputs, _ = frozen_evaluation
    weights = {p.id: p.weight for p in inputs.config.model.projection.prior_weights}
    assert weights == {"a": 0.0, "b": 1.0}
    data = json.loads((Path(__file__).parent / "fixtures/evaluation-weights.json").read_text())
    variants = tuple(
        PredictionVariant.model_validate_json(json.dumps(p)) for p in data["predictions"]
    )
    result = evaluate(inputs.model_copy(update={"predictions": variants}), "0" * 64)
    by_id = {p.id: p for p in result.variants}
    before, after = by_id["weight_B=0.5"], by_id["weight_B=1.0"]
    assert after.rank_correlation >= before.rank_correlation
    assert after.top_draft_hits >= before.top_draft_hits
    assert after.median_dollar_error <= before.median_dollar_error
    assert after.model_dump() == next(p for p in data["expected"] if p["id"] == after.id)


def test_previous_season_gp_fit_reproduces_approved_tradeoff(frozen_evaluation):
    from fba.contracts.data import Calibration
    from fba.core.projection import calibrate_availability

    inputs, _ = frozen_evaluation
    data = json.loads((Path(__file__).parent / "fixtures/evaluation-gp.json").read_text())
    fit = Calibration.model_validate_json(json.dumps(data["calibration"]))
    raw = inputs.predictions[0]
    calibrated = PredictionVariant(
        id=data["expected"]["id"],
        players=calibrate_availability(raw.players, fit, inputs.season_games, 8),
    )
    result = evaluate(inputs.model_copy(update={"predictions": (raw, calibrated)}), "0" * 64)
    before, after = result.variants
    assert after.model_dump() == data["expected"]
    assert after.rank_correlation > before.rank_correlation
    assert after.median_dollar_error < before.median_dollar_error
    assert after.games_mae < before.games_mae
    # User approved this one-player loss together with the other improvements.
    assert after.top_draft_hits == before.top_draft_hits - 1
