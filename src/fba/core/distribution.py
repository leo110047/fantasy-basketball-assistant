from itertools import product

import numpy as np
from numpy.typing import NDArray

from fba.contracts.base import DataError
from fba.contracts.config import DistributionParameters, ThresholdCount
from fba.contracts.projection import Projected, ProjectionPlayer

Floats = NDArray[np.float64]


def bounded_mean(
    capacity: Floats, weights: Floats, target: float, settings: DistributionParameters
) -> Floats:
    maximum = float(capacity.mean())
    if target < 0 or target > maximum + settings.feasibility_tolerance:
        raise DataError("projection.distribution: infeasible nested count mean")
    if target == 0:
        return np.zeros_like(capacity)
    if abs(target - maximum) < settings.feasibility_tolerance:
        return capacity.copy()
    positive = weights > 0
    if not np.any(positive) or float(capacity[positive].sum()) / len(capacity) < target:
        raise DataError("projection.distribution: missing positive calibration weights")
    low = 0.0
    high = float(np.max(capacity[positive] / weights[positive]))
    for _ in range(settings.search_iterations):
        middle = (low + high) / 2
        if float(np.minimum(capacity, middle * weights).mean()) < target:
            low = middle
        else:
            high = middle
    return np.minimum(capacity, ((low + high) / 2) * weights)


def calibrate(history: Floats, target: Floats, settings: DistributionParameters) -> Floats:
    if history.ndim != 2 or history.shape[0] == 0 or history.shape[1] != len(settings.stat_ids):
        raise DataError("projection.distribution: missing or incompatible game history")
    axes = settings.stat_ids
    nested = {pair.child for pair in settings.nested_counts}
    values = np.zeros_like(history)
    for index, field in enumerate(axes):
        if field in nested or field == settings.scoring_stat:
            continue
        average = float(history[:, index].mean())
        values[:, index] = (
            history[:, index] * (target[index] / average) if average > 0 else target[index]
        )
    for pair in settings.nested_counts:
        child, parent = axes.index(pair.child), axes.index(pair.parent)
        weights = (
            values[:, parent]
            * (history[:, child] + settings.count_pseudocount)
            / (history[:, parent] + settings.attempt_pseudocount)
        )
        values[:, child] = bounded_mean(values[:, parent], weights, float(target[child]), settings)
    values[:, axes.index(settings.scoring_stat)] = sum(
        term.coefficient * values[:, axes.index(term.stat_id)] for term in settings.scoring_terms
    )
    if not np.allclose(values.mean(axis=0), target, atol=settings.feasibility_tolerance, rtol=0):
        raise DataError("projection.distribution: calibrated means disagree with scoring target")
    return values


def discrete_states(values: Floats, settings: DistributionParameters) -> tuple[Floats, Floats]:
    """Integrate independent group uniforms, shared within each nested-count group."""
    choices = tuple(product(*(range(len(group) + 1) for group in settings.rounding_groups)))
    states = np.zeros((len(values), len(choices), len(settings.stat_ids)))
    probability = np.ones((len(values), len(choices)))
    for position, group in enumerate(settings.rounding_groups):
        indices = [settings.stat_ids.index(field) for field in group]
        fractional = values[:, indices] - np.floor(values[:, indices])
        cuts = np.sort(
            np.column_stack((np.zeros(len(values)), fractional, np.ones(len(values)))), axis=1
        )
        selection = [choice[position] for choice in choices]
        low, high = cuts[:, selection], cuts[:, [i + 1 for i in selection]]
        probability *= high - low
        for field_index, axis in enumerate(indices):
            states[:, :, axis] = np.floor(values[:, axis, None]) + (
                (low + high) / 2 < fractional[:, field_index, None]
            )
    states[:, :, settings.stat_ids.index(settings.scoring_stat)] = sum(
        term.coefficient * states[:, :, settings.stat_ids.index(term.stat_id)]
        for term in settings.scoring_terms
    )
    return states, probability / len(values)


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
        mean += (flat * weights[:, None]).sum(axis=0)
        # Fixed contiguous reductions avoid platform-specific BLAS accumulation.
        for i in range(len(mean)):
            for j in range(i, len(mean)):
                moment = float((weights * flat[:, i] * flat[:, j]).sum())
                second[i, j] += moment
                if i != j:
                    second[j, i] += moment
    covariance = second - np.outer(mean, mean)
    if not np.isfinite(mean).all() or not np.isfinite(covariance).all():
        raise DataError(f"projection.{player.id}: non-finite distribution")
    return Projected(
        id=player.id,
        expected_games=round(games, decimals),
        minutes=round(minutes, decimals),
        stats=tuple(round(float(x), decimals) for x in mean),
        covariance=tuple(tuple(round(float(x), decimals) for x in row) for row in covariance),
    )
