"""Conservative thresholds for complete lineup search (IEEE binary64)."""

import numpy as np

from fba.contracts.base import DataError
from fba.formulas.simulation import linear_interval
from fba.formulas.vector import Array, Inputs


def comparison_thresholds(inputs: Inputs) -> Array:
    """Values below which a rounded tie / win is impossible.

    Verify the boundary with the same monotone NumPy rounding operation used
    by scoring. Move down rather than assuming decimal half-way arithmetic is
    exact. One further predecessor covers rounding of a ratio division.
    """
    precision = inputs["decimals"]
    if precision.ndim or precision != np.floor(precision) or not 0 <= precision <= 12:
        raise DataError("formula.comparison_thresholds: integer precision from 0 to 12 required")
    decimals = int(precision)
    away = np.round(inputs["away"], decimals)
    if not np.isfinite(away).all():
        raise DataError("formula.comparison_thresholds: finite rounded opponent required")
    half = 0.5 * 10.0**-decimals
    threshold = np.stack((away - half, away + half), axis=-1)
    while True:
        rounded = np.round(threshold, decimals)
        unsafe = np.stack((rounded[..., 0] >= away, rounded[..., 1] > away), axis=-1)
        if not np.any(unsafe):
            return np.nextafter(threshold, -np.inf)
        threshold = np.where(unsafe, np.nextafter(threshold, -np.inf), threshold)


def linear_roundoff(inputs: Inputs) -> Array:
    """Bound statistic accumulation and the subsequent weighted linear sum.

    Nonnegative summands keep every intermediate below `upper`. Each rounded
    addition errs by at most one spacing at that bound, including subnormals.
    The absolute weighted sum bounds all product / sum intermediates of the
    category calculation. Outward arithmetic also covers this bound's work.
    """
    upper, additions = inputs["upper"], inputs["additions"]
    # A factor of two covers all orders of nonnegative binary64 accumulation
    # (n*u < 1/2), including crossing a power-of-two spacing boundary.
    if additions.ndim or additions != np.floor(additions) or not 0 <= additions <= 2**40:
        raise DataError("formula.linear_roundoff: bounded addition count required")
    if np.any(upper < 0):
        raise DataError("formula.linear_roundoff: nonnegative bound required")
    upper = np.nextafter(2 * upper, np.inf)
    weights = np.abs(inputs["weights"])
    error = np.nextafter(np.spacing(upper) * additions, np.inf)
    terms = {"indices": inputs["indices"], "weights": weights}
    propagated = linear_interval({"lower": error, "upper": error, **terms})[1]
    magnitude = linear_interval({"lower": upper, "upper": upper, **terms})[1]
    arithmetic = np.nextafter(np.spacing(magnitude) * (2 * len(weights) + 1), np.inf)
    return np.nextafter(propagated + arithmetic, np.inf)


def threshold_margin(inputs: Inputs) -> Array:
    """Upper bound on directed numerator minus threshold times denominator."""
    threshold = inputs["threshold"]
    product = np.minimum(
        threshold * inputs["dlower"][..., None],
        threshold * inputs["dupper"][..., None],
    )
    return np.nextafter(inputs["upper"][..., None] - np.nextafter(product, -np.inf), np.inf)


def threshold_error(inputs: Inputs) -> Array:
    return np.nextafter(
        inputs["numerator"][..., None]
        + np.nextafter(np.abs(inputs["threshold"]) * inputs["denominator"][..., None], np.inf),
        np.inf,
    )


def threshold_votes(inputs: Inputs) -> Array:
    # Only the sign matters. Correctly rounded addition cannot turn an exact
    # nonnegative sum negative, including underflow to signed zero.
    margin = inputs["upper"] + inputs["error"]
    votes = np.where(margin[..., 1] >= 0, 1.0, np.where(margin[..., 0] >= 0, 0.0, -1.0))
    # A ratio with zero denominator uses zero. Allow that outcome everywhere;
    # this relaxes the bound without concealing any legal zero-denominator case.
    return np.maximum(votes, inputs["zero_votes"])


def accumulation_error(inputs: Inputs) -> Array:
    """Allowance for any order of n additions bounded by an absolute sum."""
    additions = inputs["additions"]
    if additions.ndim or additions != np.floor(additions) or not 0 <= additions <= 2**40:
        raise DataError("formula.accumulation_error: bounded addition count required")
    if np.any(inputs["magnitude"] < 0):
        raise DataError("formula.accumulation_error: nonnegative magnitude required")
    magnitude = np.nextafter(2 * inputs["magnitude"], np.inf)
    return np.nextafter(np.spacing(magnitude) * additions, np.inf)


def threshold_accumulate(inputs: Inputs) -> Array:
    """Ordinary accumulation covered by the search's fixed rounding allowance."""
    return inputs["base"] + inputs["draw"]
