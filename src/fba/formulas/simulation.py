"""Numerical equations shared by season simulation and its search bounds.

Random draws, dates, roster choices and search state remain with the caller.
The registered equations consume only numeric evidence.
"""

from collections.abc import Mapping
from math import exp

import numpy as np
from numpy.typing import NDArray
from scipy.stats import norm

from fba.contracts.base import DataError
from fba.formulas.vector import Array, Inputs

type MeanInputs = Mapping[str, NDArray[np.generic]]
type NumericInputs = Mapping[str, Array | NDArray[np.bool_]]


def reduction_axes(inputs: MeanInputs) -> tuple[int, ...] | None:
    axes = inputs["axes"]
    if axes.ndim != 1 or np.any(axes != np.floor(axes)):
        raise DataError("formula.reduction: integer axes required")
    return tuple(int(v) for v in axes) if len(axes) else None


def sample_mean(inputs: MeanInputs) -> Array:
    return np.asarray(inputs["values"].mean(axis=reduction_axes(inputs)), dtype=np.float64)


def sample_variance(inputs: Inputs) -> Array:
    return np.asarray(inputs["values"].var(axis=reduction_axes(inputs)))


def sample_deviation(inputs: Inputs) -> Array:
    return np.asarray(inputs["values"].std(axis=reduction_axes(inputs)))


def linear_totals(inputs: Inputs) -> Array:
    values, weights = inputs["values"], inputs["weights"]
    indices = inputs["indices"]
    if weights.ndim != 1 or weights.shape != indices.shape:
        raise DataError("formula.linear_totals: paired statistic and coefficient axes required")
    if np.any((indices != np.floor(indices)) | (indices < 0) | (indices >= values.shape[-1])):
        raise DataError("formula.linear_totals: invalid statistic indices")
    if len(weights) == 1:
        # Preserve the established accumulation and signed-zero contract.
        return np.asarray(values[..., int(indices[0])] * weights[0] + 0.0)
    return sum(
        (
            values[..., int(i)] * coefficient
            for i, coefficient in zip(indices, weights, strict=True)
        ),
        start=np.zeros(values.shape[:-1]),
    )


def category_ratio(inputs: Inputs) -> Array:
    numerator, denominator = inputs["numerator"], inputs["denominator"]
    return np.divide(numerator, denominator, out=inputs["zero_value"].copy(), where=denominator > 0)


def game_threshold(inputs: Inputs) -> Array:
    return (
        (inputs["values"] >= inputs["threshold"]).sum(axis=-1) >= inputs["minimum_hits"]
    ).astype(np.float64)


def comparison_margin(inputs: Inputs) -> Array:
    decimals = inputs["decimals"]
    if decimals.ndim or decimals != np.floor(decimals):
        raise DataError("formula.comparison_margin: integer comparison precision required")
    return np.round(inputs["home"], int(decimals)) - np.round(inputs["away"], int(decimals))


def linear_interval(inputs: Inputs) -> Array:
    lower, upper, weights = inputs["lower"], inputs["upper"], inputs["weights"]
    indices = inputs["indices"]
    if lower.shape != upper.shape or weights.ndim != 1 or indices.shape != weights.shape:
        raise DataError("formula.linear_interval: paired bounds and coefficients required")
    if np.any((indices != np.floor(indices)) | (indices < 0) | (indices >= lower.shape[-1])):
        raise DataError("formula.linear_interval: invalid statistic indices")
    if len(weights) == 1 and abs(weights[0]) == 1:
        axis = int(indices[0])
        if weights[0] == 1:
            return np.stack((lower[..., axis], upper[..., axis]))
        return np.stack((-upper[..., axis], -lower[..., axis]))
    low, high = np.zeros(lower.shape[:-1]), np.zeros(lower.shape[:-1])
    for index, coefficient in zip(indices, weights, strict=True):
        i = int(index)
        a, b = (lower, upper) if coefficient >= 0 else (upper, lower)
        low = np.nextafter(low + np.nextafter(a[..., i] * coefficient, -np.inf), -np.inf)
        high = np.nextafter(high + np.nextafter(b[..., i] * coefficient, np.inf), np.inf)
    return np.stack((low, high))


def ratio_interval(inputs: Inputs) -> Array:
    low, high, dlow, dhigh = (inputs[k] for k in ("lower", "upper", "dlower", "dupper"))
    safe = dlow > 0
    dlow, dhigh = np.where(safe, dlow, 1.0), np.where(safe, dhigh, 1.0)
    corners = np.stack((low / dlow, low / dhigh, high / dlow, high / dhigh))
    return np.stack(
        (
            np.where(safe, np.nextafter(corners.min(axis=0), -np.inf), -np.inf),
            np.where(safe, np.nextafter(corners.max(axis=0), np.inf), np.inf),
        )
    )


def standings_credit(inputs: Inputs) -> Array:
    return inputs["wins"] + inputs["tie_value"] * inputs["ties"]


def playoff_odds(inputs: Inputs) -> Array:
    values, seeds, tolerance = inputs["scores"], inputs["seeds"], inputs["tolerance"]
    places = inputs["places"]
    if (
        values.ndim != 2
        or seeds.shape != values.shape[:1]
        or tolerance.ndim
        or tolerance <= 0
        or places.ndim
        or places != np.floor(places)
        or not 0 <= places <= len(seeds)
        or not values.shape[1]
    ):
        raise DataError("formula.playoff_odds: team/sample scores, seeds and tolerance required")
    quantized = np.rint(values / tolerance)
    indices = np.broadcast_to(np.arange(len(seeds))[:, None], values.shape)
    ordered_seeds = np.broadcast_to(seeds[:, None], values.shape)
    order = np.lexsort((indices, ordered_seeds, -quantized), axis=0)
    ranks = np.empty_like(order)
    np.put_along_axis(ranks, order, indices, axis=0)
    return (ranks < inputs["places"]).mean(axis=1)


def lognormal_taste(inputs: Inputs) -> Array:
    return np.exp(inputs["volatility"] * inputs["normal_draws"] - inputs["shift"])


def health_decay(inputs: Inputs) -> Array:
    steps = inputs["steps"]
    if steps.ndim or steps < 0 or steps != np.floor(steps):
        raise DataError("formula.health_decay: nonnegative integer game-clock steps required")
    values = np.broadcast_to(
        1 - inputs["back"] - inputs["hurt"], (int(steps) + 1, len(inputs["back"]))
    ).copy()
    values[0] = 1.0
    return np.cumprod(values, axis=0, dtype=np.float64)


def control_covariance(inputs: NumericInputs) -> Array:
    return np.asarray(
        np.einsum(
            "n,nij->ij",
            inputs["availability"] - inputs["health"].mean(axis=0),
            inputs["covariance"],
        ),
        dtype=np.float64,
    )


def normal_quantile(inputs: Inputs) -> Array:
    epsilon = inputs["epsilon"]
    if (
        epsilon.ndim
        or not 0 < epsilon < 0.5
        or np.any((inputs["uniform"] < 0) | (inputs["uniform"] > 1))
    ):
        raise DataError(
            "formula.normal_quantile: uniform probabilities and endpoint epsilon required"
        )
    return np.asarray(
        norm.ppf(np.clip(inputs["uniform"], inputs["epsilon"], 1 - inputs["epsilon"]))
    )


def scheduled_health(inputs: NumericInputs) -> Array:
    return np.asarray((inputs["scheduled"] * inputs["probabilities"]).sum(axis=0), dtype=np.float64)


def ranked_health_value(inputs: NumericInputs) -> Array:
    return np.asarray(
        np.where(inputs["health"], inputs["healthy"], inputs["unhealthy"]) * inputs["value"],
        dtype=np.float64,
    )


def portfolio_cost_floor(inputs: Inputs) -> Array:
    values, costs = inputs["values"], inputs["costs"]
    slots, player = inputs["slots"], inputs["player"]
    if slots.ndim or slots < 0 or slots != np.floor(slots):
        raise DataError("formula.portfolio_cost_floor: nonnegative integer slot count required")
    if values.ndim != 1 or costs.shape != values.shape or inputs["indices"].shape != values.shape:
        raise DataError(
            "formula.portfolio_cost_floor: paired player values, costs and indices required"
        )
    if player.ndim or inputs["multipliers"].ndim != 1 or np.any(inputs["multipliers"] < 0):
        raise DataError(
            "formula.portfolio_cost_floor: scalar player and nonnegative multiplier grid required"
        )
    spread = float(np.ptp(values))
    if spread <= 0 or slots > np.count_nonzero(inputs["indices"] != player):
        raise DataError(
            "formula.portfolio_cost_floor: positive value spread and sufficient players required"
        )
    scale = max(1.0, float(np.ptp(costs))) / spread
    multipliers = inputs["multipliers"] * scale
    scores = np.nextafter(np.nextafter(multipliers[:, None] * values, np.inf) - costs, np.inf)
    scores[:, inputs["indices"] == player] = -np.inf
    top = np.sort(scores, axis=1)[:, ::-1][:, : int(slots)]
    total = np.zeros(len(multipliers))
    for column in range(int(slots)):
        total = np.nextafter(total + top[:, column], np.inf)
    required = np.nextafter(
        np.nextafter(inputs["target"] - inputs["player_value"], -np.inf) - inputs["tolerance"],
        -np.inf,
    )
    return np.nextafter(np.nextafter(multipliers * required, -np.inf) - total, -np.inf)


def highest_bid_survival(inputs: Inputs) -> Array:
    return 1 - np.prod(inputs["cdf"], axis=0)


def median_bid(inputs: Inputs) -> Array:
    if inputs["shift"].ndim or inputs["increment"].ndim or inputs["increment"] <= 0:
        raise DataError("formula.median_bid: scalar shift and positive bid increment required")
    return (
        np.floor(
            np.minimum(
                inputs["maximum"],
                inputs["minimum"] + inputs["premium"] * exp(-float(inputs["shift"])),
            )
            / inputs["increment"]
        )
        * inputs["increment"]
    )


def mean_array(values: NDArray[np.generic], axis: int | tuple[int, ...] | None = None) -> Array:
    axes = () if axis is None else (axis,) if isinstance(axis, int) else axis
    return sample_mean({"values": values, "axes": np.asarray(axes, dtype=np.float64)})


def variance_array(values: Array, axis: int | tuple[int, ...] | None = None) -> Array:
    axes = () if axis is None else (axis,) if isinstance(axis, int) else axis
    return sample_variance({"values": values, "axes": np.asarray(axes, dtype=np.float64)})


def deviation_array(values: Array, axis: int | tuple[int, ...] | None = None) -> Array:
    axes = () if axis is None else (axis,) if isinstance(axis, int) else axis
    return sample_deviation({"values": values, "axes": np.asarray(axes, dtype=np.float64)})
