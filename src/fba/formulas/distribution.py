import numpy as np
from numpy.typing import NDArray

from fba.contracts.base import DataError
from fba.contracts.config import DistributionParameters, ThresholdCount
from fba.contracts.projection import Projected, ProjectionPlayer
from fba.formulas.arrays import evaluate_array

Floats = NDArray[np.float64]


def bounded_mean(
    capacity: Floats, weights: Floats, target: float, settings: DistributionParameters
) -> Floats:
    return evaluate_array(
        "bounded_mean",
        capacity=capacity,
        weights=weights,
        target=target,
        tolerance=settings.feasibility_tolerance,
        iterations=float(settings.search_iterations),
    ).result


def distribution_inputs(settings: DistributionParameters) -> dict[str, Floats | float]:
    axes = settings.stat_ids
    return {
        "scoring_axis": float(axes.index(settings.scoring_stat)),
        "scoring_terms": np.array(
            [(axes.index(t.stat_id), t.coefficient) for t in settings.scoring_terms],
            dtype=np.float64,
        ),
    }


def calibrate(history: Floats, target: Floats, settings: DistributionParameters) -> Floats:
    if history.ndim != 2 or history.shape[1] != len(settings.stat_ids):
        raise DataError("projection.distribution: incompatible history axes")
    return evaluate_array(
        "calibrate_distribution",
        history=history,
        target=target,
        nested=np.array(
            [
                (settings.stat_ids.index(p.child), settings.stat_ids.index(p.parent))
                for p in settings.nested_counts
            ],
            dtype=np.float64,
        ).reshape((-1, 2)),
        count_pseudocount=settings.count_pseudocount,
        attempt_pseudocount=settings.attempt_pseudocount,
        tolerance=settings.feasibility_tolerance,
        iterations=float(settings.search_iterations),
        **distribution_inputs(settings),
    ).result


def discrete_states(values: Floats, settings: DistributionParameters) -> tuple[Floats, Floats]:
    groups = np.array(
        [[float(s in group) for s in settings.stat_ids] for group in settings.rounding_groups],
        dtype=np.float64,
    )
    result = evaluate_array(
        "rounding_distribution", values=values, groups=groups, **distribution_inputs(settings)
    ).result
    return result[..., :-1], result[..., -1]


def moments(
    player: ProjectionPlayer,
    games: float,
    minutes: float,
    target: tuple[float, ...],
    settings: DistributionParameters,
    threshold: ThresholdCount,
    decimals: int,
) -> Projected:
    if not player.history or any(len(row) != len(settings.stat_ids) for row in player.history):
        raise DataError(f"projection.{player.id}.history: missing or incompatible game rows")
    if len(target) != len(settings.stat_ids):
        raise DataError(f"projection.{player.id}.target: incompatible statistic axes")
    values = calibrate(
        np.array(player.history, dtype=np.float64), np.array(target, dtype=np.float64), settings
    )
    axes = [settings.stat_ids.index(field) for field in threshold.stat_ids]
    mean = np.zeros(len(settings.stat_ids) + 1)
    second = np.zeros((len(mean), len(mean)))
    for offset in range(0, len(values), settings.integration_batch_size):
        batch = values[offset : offset + settings.integration_batch_size]
        states, probability = discrete_states(batch, settings)
        hits = (states[:, :, axes] >= threshold.threshold).sum(axis=2)
        combined = np.concatenate((states, (hits >= threshold.minimum_hits)[:, :, None]), axis=2)
        flat = combined.reshape((-1, combined.shape[-1]))
        weights = probability.reshape(-1) * (len(batch) / len(values))
        mean += evaluate_array("weighted_rows", values=flat, weights=weights).result
        second += evaluate_array("weighted_second", values=flat, weights=weights).result
    covariance = evaluate_array("centered_covariance", mean=mean, second=second).result
    if not np.isfinite(mean).all() or not np.isfinite(covariance).all():
        raise DataError(f"projection.{player.id}: non-finite distribution")
    return Projected(
        id=player.id,
        expected_games=round(games, decimals),
        minutes=round(minutes, decimals),
        stats=tuple(round(float(x), decimals) for x in mean),
        covariance=tuple(tuple(round(float(x), decimals) for x in row) for row in covariance),
    )
