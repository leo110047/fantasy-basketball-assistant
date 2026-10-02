"""Vector equations referenced by the shared executable registry."""

from collections.abc import Mapping
from fractions import Fraction
from itertools import product

import numpy as np
from numpy.typing import NDArray
from scipy.special import expit, ndtr

from fba.contracts.base import DataError

type Array = NDArray[np.float64]
type Inputs = dict[str, Array]
type MatrixInputs = Mapping[str, Array | NDArray[np.bool_]]


def availability_regression(inputs: Inputs) -> Array:
    projected, observed = inputs["projected"], inputs["observed"]
    if projected.ndim != 1 or observed.shape != projected.shape or len(projected) < 2:
        raise DataError("calibration: need at least two paired game counts")
    # Preserve the original exact decimal-rational accumulation.
    x = [Fraction(str(float(v))) for v in projected]
    if any(int(v) != v or v < 0 for v in observed):
        raise DataError("calibration: observed games must be nonnegative integers")
    y = [Fraction(int(v)) for v in observed]
    n = len(x)
    sx, sy = sum(x), sum(y)
    denominator = n * sum(v * v for v in x) - sx * sx
    if denominator == 0:
        raise DataError("calibration: projected games have zero variance")
    slope = (n * sum(a * b for a, b in zip(x, y, strict=True)) - sx * sy) / denominator
    intercept = (sy - slope * sx) / n
    return np.array([float(intercept), float(slope)])


def standard_deviation_floor(inputs: Inputs) -> Array:
    if inputs["values"].ndim < 1 or inputs["fraction"].ndim:
        raise DataError("formula.standard_deviation_floor: samples and scalar fraction required")
    if inputs["fraction"] < 0 or np.any(inputs["floor"] <= 0):
        raise DataError(
            "formula.standard_deviation_floor: positive floor and nonnegative fraction required"
        )
    return np.maximum(inputs["values"].std(axis=0) * inputs["fraction"], inputs["floor"])


def central_difference(inputs: Inputs) -> Array:
    if np.any(inputs["step"] <= 0):
        raise DataError("formula.central_difference: positive step required")
    return (inputs["plus"] - inputs["minus"]) / (2 * inputs["step"])


def utility_rescale(inputs: Inputs) -> Array:
    scale = inputs["scale"]
    if scale.ndim or scale <= 0 or not inputs["reference"].size:
        raise DataError("formula.utility_rescale: positive scale and reference required")
    return inputs["values"] / scale * inputs["reference"].std()


def matrix_product(inputs: MatrixInputs) -> Array:
    return np.asarray(inputs["left"] @ inputs["right"], dtype=np.float64)


def bernoulli_mean(inputs: Inputs) -> Array:
    return inputs["means"] * inputs["probability"][..., None]


def bernoulli_covariance(inputs: Inputs) -> Array:
    q, mean = inputs["probability"], inputs["means"]
    if mean.ndim != 2 or q.shape != mean.shape[:1] or np.any((q < 0) | (q > 1)):
        raise DataError("formula.bernoulli_covariance: paired means and probabilities required")
    return inputs["covariance"] * q[:, None, None] + np.einsum(
        "ni,nj,n->nij", mean, mean, q * (1 - q)
    )


def availability_value(inputs: Inputs) -> Array:
    if np.any(inputs["floor"] <= 0):
        raise DataError("formula.availability_value: positive denominator floor required")
    return inputs["priority"] / np.maximum(inputs["availability"], inputs["floor"])


def sample_covariance(inputs: Inputs) -> Array:
    values = inputs["values"]
    if values.ndim < 2 or not len(values):
        raise DataError("formula.sample_covariance: sample and statistic axes required")
    delta = values - values.mean(axis=0)
    return np.einsum("s...i,s...j->...ij", delta, delta) / max(1, len(values) - 1)


def control_variate(inputs: Inputs) -> Array:
    return inputs["physical"] - (inputs["realized"] - inputs["expected"])


def stratified_uniform(inputs: Inputs) -> Array:
    jitter = inputs["jitter"]
    if jitter.ndim < 1 or not len(jitter) or np.any((jitter < 0) | (jitter >= 1)):
        raise DataError("formula.stratified_uniform: nonempty uniform jitter in [0, 1) required")
    index = np.arange(len(jitter)).reshape((len(jitter),) + (1,) * (jitter.ndim - 1))
    return (index + jitter) / len(jitter)


def row_rates(inputs: Inputs) -> Array:
    counts, minutes = inputs["counts"], inputs["minutes"]
    if counts.ndim != 2 or minutes.shape != counts.shape[:1] or np.any(minutes <= 0):
        raise DataError("formula.row_rates: paired counts and positive minutes required")
    return counts / minutes[:, None]


def covariance_root(inputs: Inputs) -> Array:
    covariance = inputs["covariance"]
    if covariance.ndim != 2 or covariance.shape[0] != covariance.shape[1]:
        raise DataError("formula.covariance_root: covariance must be square")
    values, vectors = np.linalg.eigh((covariance + covariance.T) / 2)
    return (vectors * np.sqrt(np.maximum(values, 0))) @ vectors.T


def soft_majority(inputs: Inputs) -> Array:
    differences, bandwidth = inputs["differences"], inputs["bandwidth"]
    if differences.ndim < 1 or differences.shape[-1] == 0 or bandwidth.ndim or bandwidth <= 0:
        raise DataError("formula.soft_majority: category samples and positive bandwidth required")
    pivot = (differences.shape[-1] - 1) // 2
    return expit(np.partition(differences, pivot, axis=-1)[..., pivot] / bandwidth)


def category_marginals(inputs: Inputs) -> Array:
    values, bandwidth, step = inputs["differences"], inputs["bandwidth"], inputs["step"]
    if values.ndim != 2 or values.shape[0] == 0 or step.ndim or step <= 0:
        raise DataError("formula.category_marginals: samples and positive gradient step required")
    slopes = np.empty(values.shape[1])
    for index in range(values.shape[1]):
        plus, minus = values.copy(), values.copy()
        plus[:, index] += step
        minus[:, index] -= step
        slopes[index] = central_difference(
            {
                "plus": np.asarray(
                    soft_majority({"differences": plus, "bandwidth": bandwidth}).mean()
                ),
                "minus": np.asarray(
                    soft_majority({"differences": minus, "bandwidth": bandwidth}).mean()
                ),
                "step": step,
            }
        )
    total = float(slopes.sum())
    weights = slopes / total * values.shape[1] if total > 0 else np.zeros_like(slopes)
    return np.stack(((values > 0).mean(axis=0), weights), axis=-1)


def bootstrap_scale(inputs: Inputs) -> Array:
    source, samples, target, fallback = (
        inputs[k] for k in ("history", "samples", "target", "zero_mean_draws")
    )
    if source.ndim != 2 or not len(source) or target.shape != source.shape[1:]:
        raise DataError("formula.bootstrap_scale: history and target axes differ")
    if samples.ndim != 2 or samples.shape[1:] != target.shape or fallback.shape != samples.shape:
        raise DataError("formula.bootstrap_scale: sample axes differ")
    mean = source.mean(axis=0)
    result = fallback.copy()
    np.divide(samples * target, mean, out=result, where=mean > 0)
    return result


def sampling_covariance(inputs: Inputs) -> Array:
    means, covariance = inputs["means"], inputs["covariances"]
    if means.ndim != 2 or covariance.shape != (len(means), means.shape[-1], means.shape[-1]):
        raise DataError("formula.sampling_covariance: paired mean/covariance samples required")
    if not len(means):
        raise DataError("formula.sampling_covariance: no samples")
    return covariance.mean(axis=0) + np.cov(means, rowvar=False, bias=True)


def standardized_margins(inputs: Inputs) -> Array:
    if np.any(inputs["scale"] <= 0):
        raise DataError("formula.standardized_margins: positive scales required")
    return (inputs["values"] - inputs["opponent"]) / inputs["scale"]


def logistic_objective(inputs: Inputs) -> Array:
    x, beta, y = (inputs[k] for k in ("features", "coefficients", "outcomes"))
    if x.ndim != 2 or not len(x) or beta.shape != x.shape[1:] or y.shape != x.shape[:1]:
        raise DataError("formula.logistic_objective: paired feature/outcome samples required")
    z = x @ beta
    return np.asarray(np.mean(np.logaddexp(0.0, z) - y * z), dtype=np.float64)


def logistic_gradient(inputs: Inputs) -> Array:
    x, beta, y = (inputs[k] for k in ("features", "coefficients", "outcomes"))
    if x.ndim != 2 or not len(x) or beta.shape != x.shape[1:] or y.shape != x.shape[:1]:
        raise DataError("formula.logistic_gradient: paired feature/outcome samples required")
    return np.asarray(x.T @ (expit(x @ beta) - y) / len(y), dtype=np.float64)


def logistic_hessian(inputs: Inputs) -> Array:
    x, beta = inputs["features"], inputs["coefficients"]
    if x.ndim != 2 or not len(x) or beta.shape != x.shape[1:]:
        raise DataError("formula.logistic_hessian: feature matrix and coefficients required")
    p = expit(x @ beta)
    return np.asarray(x.T @ (x * (p * (1 - p))[:, None]) / len(x), dtype=np.float64)


def calibration_fit(inputs: Inputs) -> Array:
    x, y = inputs["predicted"] - 0.5, inputs["observed"] - 0.5
    if x.ndim != 1 or not len(x) or x.shape != y.shape:
        raise DataError("formula.calibration_fit: nonempty paired training outcomes required")
    denominator = float(x @ x)
    return np.asarray(min(1.0, max(0.0, float(x @ y) / denominator)) if denominator > 0 else 0.0)


def health_transitions(inputs: Inputs) -> Array:
    q, missed, floor = (inputs[k] for k in ("availability", "mean_missed", "floor"))
    if np.any((q < 0) | (q > 1)) or missed.ndim or missed <= 0 or floor.ndim or floor <= 0:
        raise DataError("formula.health_transitions: invalid probability or duration")
    back = np.minimum(1 / missed, q / np.maximum(1 - q, floor))
    hurt = (1 - q) / np.maximum(q, floor) * back
    return np.stack((back, hurt))


def conditional_health(inputs: Inputs) -> Array:
    return np.clip(
        inputs["availability"] + (inputs["status"] - inputs["availability"]) * inputs["decay"],
        0,
        1,
    )


def health_count_covariance(inputs: Inputs) -> Array:
    q, decay, stats = (inputs[k] for k in ("availability", "decay", "stats"))
    if q.ndim or not 0 <= q <= 1 or stats.ndim != 1:
        raise DataError("formula.health_count_covariance: invalid probability or statistic vector")
    variance = q * (1 - q) * decay.sum()
    return variance * np.outer(stats, stats)


def managed_covariance(inputs: Inputs) -> Array:
    counts, covariance, physical, correction = (
        inputs[k] for k in ("counts", "covariance", "physical", "correction")
    )
    if counts.ndim != 4 or physical.ndim != 4 or counts.shape[:-1] != physical.shape[:-1]:
        raise DataError("formula.managed_covariance: health/team/week axes differ")
    within = np.einsum("twn,nij->twij", counts.mean(axis=0), covariance)
    delta = physical - physical.mean(axis=0)
    return (
        within + np.einsum("stwi,stwj->twij", delta, delta) / max(1, len(counts) - 1) + correction
    )


def utility_blend(inputs: Inputs) -> Array:
    step = inputs["step"]
    if step.ndim or not 0 <= step <= 1:
        raise DataError("formula.utility_blend: step must be a probability")
    return (1 - step) * inputs["baseline"] + step * inputs["managed"]


def bid_distribution(inputs: Inputs) -> Array:
    levels, maximum, premium, volatility, shift, minimum, increment = (
        inputs[k]
        for k in ("levels", "maximum", "premium", "volatility", "shift", "minimum", "increment")
    )
    threshold = np.maximum(0.0, levels + increment - minimum)
    ratio = np.divide(
        threshold[None, :],
        premium[:, None],
        out=np.full((len(premium), len(levels)), np.inf),
        where=premium[:, None] > 0,
    )
    if volatility == 0:
        probability = (
            minimum + premium[:, None] * np.exp(-shift) < levels[None, :] + increment
        ).astype(float)
    else:
        logarithm = np.full(ratio.shape, -np.inf)
        np.log(ratio, out=logarithm, where=ratio > 0)
        probability = ndtr((logarithm + shift) / volatility)
    return np.where(
        levels[None, :] < minimum,
        0.0,
        np.where(levels[None, :] >= maximum[:, None], 1.0, probability),
    )


def second_bid_survival(inputs: Inputs) -> Array:
    cdf, previous = inputs["cdf"], inputs["previous"]
    # At level b, the sale price exceeds b unless everybody bids <= b, or
    # exactly one bids > b while all the others bid < b (one increment below).
    edge = np.ones((1, cdf.shape[1]))
    prefix = np.concatenate((edge, np.cumprod(previous, axis=0)), axis=0)
    suffix = np.concatenate((np.cumprod(previous[::-1], axis=0)[::-1], edge), axis=0)
    single = np.sum((1 - cdf) * prefix[:-1] * suffix[1:], axis=0)
    return np.clip(1 - np.prod(cdf, axis=0) - single, 0, 1)


def bounded_mean(inputs: Inputs) -> Array:
    capacity, weights = inputs["capacity"], inputs["weights"]
    target, tolerance = float(inputs["target"]), float(inputs["tolerance"])
    iterations = int(inputs["iterations"])
    if capacity.ndim != 1 or not len(capacity) or capacity.shape != weights.shape:
        raise DataError("formula.bounded_mean: paired capacity/weight vectors required")
    if iterations < 1 or iterations != inputs["iterations"] or tolerance <= 0:
        raise DataError("formula.bounded_mean: positive tolerance and integer iterations required")
    maximum = float(capacity.mean())
    if target < 0 or target > maximum + tolerance:
        raise DataError("projection.distribution: infeasible nested count mean")
    if target == 0:
        return np.zeros_like(capacity)
    if abs(target - maximum) < tolerance:
        return capacity.copy()
    positive = weights > 0
    if not np.any(positive) or float(capacity[positive].sum()) / len(capacity) < target:
        raise DataError("projection.distribution: missing positive calibration weights")
    low = 0.0
    high = float(np.max(capacity[positive] / weights[positive]))
    for _ in range(iterations):
        middle = (low + high) / 2
        if float(np.minimum(capacity, middle * weights).mean()) < target:
            low = middle
        else:
            high = middle
    return np.minimum(capacity, ((low + high) / 2) * weights)


def weighted_rows(inputs: Inputs) -> Array:
    values, weights = inputs["values"], inputs["weights"]
    if values.ndim != 2 or weights.shape != values.shape[:1]:
        raise DataError("formula.weighted_rows: row weights do not match observations")
    return (values * weights[:, None]).sum(axis=0)


def weighted_second(inputs: Inputs) -> Array:
    values, weights = inputs["values"], inputs["weights"]
    if values.ndim != 2 or weights.shape != values.shape[:1]:
        raise DataError("formula.weighted_second: row weights do not match observations")
    result = np.zeros((values.shape[1], values.shape[1]))
    # Fixed reductions preserve the cross-platform accumulation contract.
    for i in range(len(result)):
        for j in range(i, len(result)):
            result[i, j] = result[j, i] = float((weights * values[:, i] * values[:, j]).sum())
    return result


def centered_covariance(inputs: Inputs) -> Array:
    return inputs["second"] - np.outer(inputs["mean"], inputs["mean"])


def category_points(inputs: Inputs) -> Array:
    differences, ties = inputs["differences"], inputs["ties"]
    return (differences > 0).astype(np.float64) + (differences == 0) * ties


def week_points(inputs: Inputs) -> Array:
    differences = inputs["differences"]
    # The same tie credits occur on both sides and cancel exactly. Compute
    # signed category wins once instead of allocating category-point tensors.
    margin = (differences > 0).sum(axis=-1) - (differences < 0).sum(axis=-1)
    return week_outcome(margin, inputs["week_tie"])


def week_outcome(margin: Array, week_tie: Array) -> Array:
    return np.asarray((margin > 0).astype(np.float64) + (margin == 0) * week_tie)


def scoring_axes(inputs: Inputs, width: int) -> tuple[int, Array]:
    axis, terms = inputs["scoring_axis"], inputs["scoring_terms"]
    if axis.ndim or axis != np.floor(axis) or not 0 <= axis < width:
        raise DataError("formula.scoring_axis: invalid integer axis")
    if terms.ndim != 2 or terms.shape[1] != 2 or not len(terms):
        raise DataError("formula.scoring_terms: ordered axis/coefficient pairs required")
    indices = terms[:, 0]
    if np.any((indices != np.floor(indices)) | (indices < 0) | (indices >= width)):
        raise DataError("formula.scoring_terms: invalid integer axis")
    return int(axis), terms


def calibrate_distribution(inputs: Inputs) -> Array:
    history, target, nested = inputs["history"], inputs["target"], inputs["nested"]
    if history.ndim != 2 or not len(history) or target.shape != history.shape[1:]:
        raise DataError("formula.calibrate_distribution: paired history/target axes required")
    scoring, terms = scoring_axes(inputs, history.shape[1])
    if not nested.size:
        nested = nested.reshape((0, 2))
    if (
        nested.ndim != 2
        or nested.shape[1] != 2
        or np.any((nested != np.floor(nested)) | (nested < 0) | (nested >= history.shape[1]))
    ):
        raise DataError("formula.calibrate_distribution: invalid nested axis pairs")
    values = np.zeros_like(history)
    for index in range(history.shape[1]):
        if index in nested[:, 0] or index == scoring:
            continue
        average = float(history[:, index].mean())
        values[:, index] = (
            history[:, index] * (target[index] / average) if average > 0 else target[index]
        )
    for child_index, parent_index in nested:
        child, parent = int(child_index), int(parent_index)
        weights = (
            values[:, parent]
            * (history[:, child] + inputs["count_pseudocount"])
            / (history[:, parent] + inputs["attempt_pseudocount"])
        )
        values[:, child] = bounded_mean(
            {
                "capacity": values[:, parent],
                "weights": weights,
                "target": np.asarray(target[child]),
                "tolerance": inputs["tolerance"],
                "iterations": inputs["iterations"],
            }
        )
    values[:, scoring] = sum(coefficient * values[:, int(index)] for index, coefficient in terms)
    if not np.allclose(values.mean(axis=0), target, atol=float(inputs["tolerance"]), rtol=0):
        raise DataError("projection.distribution: calibrated means disagree with scoring target")
    return values


def rounding_distribution(inputs: Inputs) -> Array:
    values = inputs["values"]
    membership = inputs["groups"]
    if values.ndim != 2 or not len(values) or membership.ndim != 2:
        raise DataError("formula.rounding_distribution: nonempty values/groups required")
    scoring, terms = scoring_axes(inputs, values.shape[1])
    if (
        membership.shape[1] != values.shape[1]
        or not len(membership)
        or np.any((membership != 0) & (membership != 1))
        or np.any(membership.sum(axis=0) > 1)
        or membership[:, scoring].any()
    ):
        raise DataError("formula.rounding_distribution: invalid group membership")
    groups = [np.flatnonzero(row) for row in membership]
    if any(not len(group) for group in groups):
        raise DataError("formula.rounding_distribution: empty rounding group")
    choices = tuple(product(*(range(len(group) + 1) for group in groups)))
    states = np.zeros((len(values), len(choices), values.shape[1]))
    probability = np.ones((len(values), len(choices)))
    for position, indices in enumerate(groups):
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
    states[:, :, scoring] = sum(
        coefficient * states[:, :, int(index)] for index, coefficient in terms
    )
    return np.concatenate((states, (probability / len(values))[..., None]), axis=-1)


def average_ranks(inputs: Inputs) -> Array:
    values = inputs["values"]
    if values.ndim != 1:
        raise DataError("formula.average_ranks: one value per observation required")
    order = sorted(range(len(values)), key=lambda i: (values[i], i))
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + end - 1) / 2
        for position in range(start, end):
            result[order[position]] = rank
        start = end
    return np.asarray(result, dtype=np.float64)
