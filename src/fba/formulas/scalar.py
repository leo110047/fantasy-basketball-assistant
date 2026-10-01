"""Scalar equations referenced by the shared executable registry."""

from math import erf, exp, floor, fsum, inf, isfinite, log, pi, sqrt

import numpy as np
from scipy.integrate import quad
from scipy.special import log_ndtr

from fba.contracts.base import DataError
from fba.contracts.formula import ScalarInputs
from fba.formulas.vector import average_ranks


def number(values: ScalarInputs, key: str) -> float:
    value = values[key]
    if isinstance(value, tuple):
        raise DataError(f"formula.{key}: expected a number")
    return value


def vector(values: ScalarInputs, key: str) -> tuple[float, ...]:
    value = values[key]
    if not isinstance(value, tuple):
        raise DataError(f"formula.{key}: expected a vector")
    return value


def blend(values: ScalarInputs) -> float:
    k, prior, total, sample = (number(values, k) for k in ("k", "prior", "total", "sample"))
    return (k * prior + total) / (k + sample)


def minutes(values: ScalarInputs) -> float:
    rows = vector(values, "minutes")
    half_life = number(values, "half_life")
    weights = tuple(0.5 ** ((len(rows) - i - 1) / half_life) for i in range(len(rows)))
    return blend(
        {
            "k": number(values, "k"),
            "prior": number(values, "prior"),
            "total": fsum(w * m for w, m in zip(weights, rows, strict=True)),
            "sample": fsum(weights),
        }
    )


def sample_weight(values: ScalarInputs) -> float:
    return number(values, "sample") / (number(values, "sample") + number(values, "k"))


def expectation(values: ScalarInputs) -> float:
    return number(values, "q") * number(values, "rate") * number(values, "minutes")


def shrink(values: ScalarInputs) -> float:
    return 0.5 + number(values, "c") * (number(values, "p") - 0.5)


def score_calibration(values: ScalarInputs) -> float:
    midpoint = number(values, "scale") / 2
    return midpoint + number(values, "c") * (number(values, "score") - midpoint)


def z_score(values: ScalarInputs) -> float:
    delta = number(values, "home") - number(values, "away")
    variance = number(values, "home_variance") + number(values, "away_variance")
    # A zero-variance comparison has no finite z. Use the documented numerical
    # limit from configuration; normal_probability still handles exact ties.
    if variance == 0:
        return 0.0 if delta == 0 else number(values, "limit") * (1 if delta > 0 else -1)
    return delta / sqrt(variance)


def normal_probability(values: ScalarInputs) -> float:
    return (1 + erf(number(values, "z") / sqrt(2))) / 2


def sampling_error(values: ScalarInputs) -> float:
    return sqrt(number(values, "variance") / number(values, "samples"))


def empirical_error(values: ScalarInputs) -> float:
    rows = vector(values, "values")
    if len(rows) < 2:
        raise DataError("formula.empirical_error: at least two observations required")
    center = mean(values)
    return sqrt(fsum((v - center) ** 2 for v in rows) / (len(rows) - 1) / len(rows))


def exposure_rate(values: ScalarInputs) -> float:
    counts, exposure = vector(values, "counts"), vector(values, "exposure")
    if len(counts) != len(exposure) or not exposure or fsum(exposure) <= 0:
        raise DataError("formula.exposure_rate: positive total exposure is required")
    return fsum(counts) / fsum(exposure)


def exposure_error(values: ScalarInputs) -> float:
    counts, exposure = vector(values, "counts"), vector(values, "exposure")
    if len(counts) < 2 or len(counts) != len(exposure) or fsum(exposure) <= 0:
        raise DataError("formula.exposure_error: two games and positive exposure are required")
    rate = exposure_rate(values)
    total = fsum(exposure)
    # Cluster-by-game error handles varying minutes and overdispersion. The
    # Poisson model floor retains uncertainty with all-zero or identical logs.
    residual = fsum((c - rate * m) ** 2 for c, m in zip(counts, exposure, strict=True))
    robust = len(counts) / (len(counts) - 1) * residual / total**2
    return sqrt(max(robust, max(rate, number(values, "model")) / total))


def monitor_margin(values: ScalarInputs) -> float:
    return number(values, "z") / (2 * sqrt(number(values, "samples")))


def effective_samples(values: ScalarInputs) -> float:
    weights = vector(values, "weights")
    if not weights or any(w <= 0 for w in weights):
        raise DataError("formula.effective_samples: positive cluster weights are required")
    return fsum(weights) ** 2 / fsum(w * w for w in weights)


def probability(values: ScalarInputs) -> float:
    return (number(values, "wins") + number(values, "ties") * number(values, "tie")) / number(
        values, "samples"
    )


def add_score(values: ScalarInputs) -> float:
    return number(values, "delta_week") + number(values, "weight") * number(values, "delta_season")


def difference(values: ScalarInputs) -> float:
    return number(values, "after") - number(values, "before")


def rank_value(values: ScalarInputs) -> float:
    return number(values, "scale") / number(values, "rank") ** number(values, "exponent")


def trade_value_ratio(values: ScalarInputs) -> float:
    home, away = number(values, "home"), number(values, "away")
    if min(home, away) <= 0:
        raise DataError("formula.trade_value_ratio: both bundle values must be positive")
    return min(home, away) / max(home, away)


def acceptance(values: ScalarInputs) -> float:
    z = (
        number(values, "beta_rank") * number(values, "delta_rank")
        + number(values, "beta_need") * number(values, "delta_need")
        - number(values, "threshold")
    ) / number(values, "noise")
    return 1 / (1 + exp(-z)) if z >= 0 else exp(z) / (1 + exp(z))


def product(values: ScalarInputs) -> float:
    return number(values, "gain") * number(values, "probability")


def brier(values: ScalarInputs) -> float:
    ps, ys = vector(values, "predicted"), vector(values, "observed")
    return fsum((p - y) ** 2 for p, y in zip(ps, ys, strict=True)) / len(ps)


def log_loss(values: ScalarInputs) -> float:
    ps, ys = vector(values, "predicted"), vector(values, "observed")
    epsilon = number(values, "epsilon")
    clipped = (min(1 - epsilon, max(epsilon, p)) for p in ps)
    return -fsum(y * log(p) + (1 - y) * log(1 - p) for p, y in zip(clipped, ys, strict=True)) / len(
        ps
    )


def mean_absolute_error(values: ScalarInputs) -> float:
    ps, ys = vector(values, "predicted"), vector(values, "observed")
    return fsum(abs(p - y) for p, y in zip(ps, ys, strict=True)) / len(ps)


def reallocate(values: ScalarInputs) -> float:
    return number(values, "current") - number(values, "delta") * number(values, "current") / number(
        values, "team_total"
    )


def weighted_sum(values: ScalarInputs) -> float:
    return fsum(
        v * w for v, w in zip(vector(values, "values"), vector(values, "weights"), strict=True)
    )


def total_ratio(values: ScalarInputs) -> float:
    denominator = number(values, "denominator")
    return (
        number(values, "numerator") / denominator if denominator else number(values, "zero_value")
    )


def threshold_count(values: ScalarInputs) -> float:
    return float(
        sum(v >= number(values, "threshold") for v in vector(values, "values"))
        >= number(values, "minimum_hits")
    )


def mean(values: ScalarInputs) -> float:
    rows = vector(values, "values")
    return fsum(rows) / len(rows)


def weighted_mean(values: ScalarInputs) -> float:
    weights = vector(values, "weights")
    if not weights or any(w < 0 for w in weights) or fsum(weights) <= 0:
        raise DataError("formula.weighted_mean: nonnegative weights with positive total required")
    return weighted_sum(values) / fsum(weights)


def complementary_value(values: ScalarInputs) -> float:
    return -fsum(
        (a - 0.5) * (b - 0.5)
        for a, b in zip(vector(values, "home"), vector(values, "away"), strict=True)
    )


def path_bid(values: ScalarInputs) -> float:
    minimum = number(values, "minimum")
    willingness = minimum + max(0.0, number(values, "anchor") - minimum) * number(
        values, "inflation"
    ) * number(values, "participation") * number(values, "taste") * number(values, "premium")
    increment = number(values, "increment")
    return floor(min(number(values, "maximum"), willingness) / increment) * increment


def historical_share(values: ScalarInputs) -> float:
    return number(values, "parent") * number(values, "numerator") / number(values, "denominator")


def population_variance(values: ScalarInputs) -> float:
    center = number(values, "mean")
    rows = vector(values, "values")
    return fsum((x - center) ** 2 for x in rows) / len(rows)


def standardize(values: ScalarInputs) -> float:
    return (number(values, "value") - number(values, "mean")) / number(values, "deviation")


def ratio_impact(values: ScalarInputs) -> float:
    return number(values, "direction") * (
        number(values, "made") - number(values, "rate") * number(values, "attempted")
    )


def season_utility(values: ScalarInputs) -> float:
    return (
        (fsum(vector(values, "scores")) - number(values, "replacement"))
        * number(values, "games")
        / number(values, "season_games")
    )


def dollar_scale(values: ScalarInputs) -> float:
    return (
        number(values, "teams") * number(values, "budget")
        - number(values, "roster_count") * number(values, "minimum")
    ) / fsum(vector(values, "positive_utilities"))


def dollar_value(values: ScalarInputs) -> float:
    utility = number(values, "utility")
    return number(values, "minimum") + utility * number(values, "scale") if utility > 0 else 0.0


def bidder_wealth(values: ScalarInputs) -> float:
    surplus = (
        number(values, "budget") / number(values, "slots") - number(values, "minimum")
    ) / number(values, "average_surplus")
    return min(number(values, "upper"), max(number(values, "lower"), surplus)) ** number(
        values, "exponent"
    )


def clipped_anchor(values: ScalarInputs) -> float:
    quote, minimum = number(values, "quote"), number(values, "minimum")
    return (
        min(number(values, "maximum"), max(minimum, quote * number(values, "scale")))
        if quote >= number(values, "cutoff")
        else minimum
    )


def market_inflation(values: ScalarInputs) -> float:
    denominator = number(values, "anchor_total")
    return number(values, "cash") / denominator if denominator > 0 else 1.0


def sale_cost(values: ScalarInputs) -> float:
    return number(values, "minimum") + number(values, "increment") * fsum(
        vector(values, "survival")
    )


def winning_cost(values: ScalarInputs) -> float:
    survival = vector(values, "survival")
    return number(values, "minimum") + number(values, "increment") * (
        fsum(survival) + (survival[0] if survival else 0.0)
    )


def planning_cost(values: ScalarInputs) -> float:
    increment = number(values, "increment")
    return max(
        number(values, "minimum"),
        floor(number(values, "acquisition") / increment + 0.5) * increment,
    )


def affordable_cap(values: ScalarInputs) -> float:
    increment = number(values, "increment")
    amount = min(
        number(values, "budget") - (number(values, "slots") - 1) * number(values, "minimum"),
        number(values, "budget") - number(values, "completion_cost"),
    )
    return amount // increment * increment


def market_normalization(values: ScalarInputs) -> float:
    teams, volatility = (number(values, k) for k in ("teams", "volatility"))
    if teams < 2 or not float(teams).is_integer() or volatility < 0:
        raise DataError("formula.market_normalization: invalid bidder count or volatility")
    if volatility == 0:
        return 0.0

    def integrand(z: float) -> float:
        return exp(
            volatility * z
            + log(teams * (teams - 1))
            + (teams - 2) * float(log_ndtr(z))
            + float(log_ndtr(-z))
            - z * z / 2
            - log(2 * pi) / 2
        )

    try:
        report = quad(integrand, -inf, inf, full_output=1, epsabs=1e-10, epsrel=1e-10)
        value, error, _diagnostics, *message = report
        if message or not isfinite(value) or value <= 0 or error > 1e-8 * max(1.0, value):
            raise ArithmeticError("normalization did not converge")
        return log(value)
    except (ArithmeticError, ValueError) as error:
        raise DataError("formula.market_normalization: cannot resolve bid normalization") from error


def healthy_capacity(values: ScalarInputs) -> float:
    expected, eligible, share = (
        number(values, k) for k in ("expected", "eligible", "injury_share")
    )
    return min(eligible, max(expected, eligible - (eligible - expected) * share))


def clipped_affine(values: ScalarInputs) -> float:
    estimate = number(values, "intercept") + number(values, "slope") * number(values, "value")
    return min(number(values, "upper"), max(number(values, "lower"), estimate))


def budget_fraction(values: ScalarInputs) -> float:
    demand = number(values, "demand")
    return min(1.0, number(values, "budget") / demand) if demand else 1.0


def rank_correlation(values: ScalarInputs) -> float:
    x, y = vector(values, "predicted"), vector(values, "observed")
    if len(x) != len(y) or len(x) < 2:
        raise DataError("evaluation.common_ids: at least two paired observations required")
    a, b = (average_ranks({"values": np.asarray(v, dtype=np.float64)}) for v in (x, y))
    ma, mb = fsum(a) / len(a), fsum(b) / len(b)
    denominator = sqrt(fsum((v - ma) ** 2 for v in a) * fsum((v - mb) ** 2 for v in b))
    if denominator == 0:
        raise DataError("evaluation: rank correlation undefined for constant values")
    return fsum((v - ma) * (w - mb) for v, w in zip(a, b, strict=True)) / denominator


def median(inputs: ScalarInputs) -> float:
    values = vector(inputs, "values")
    if not values:
        raise DataError("formula.median: nonempty observations required")
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def second_price(inputs: ScalarInputs) -> float:
    return min(number(inputs, "winner"), number(inputs, "runner_up") + number(inputs, "increment"))


def minute_budget(inputs: ScalarInputs) -> float:
    return number(inputs, "players") * (number(inputs, "regulation") + number(inputs, "overtime"))


def season_rate(inputs: ScalarInputs) -> float:
    return number(inputs, "games") * number(inputs, "value") / number(inputs, "season_games")


def anchor_scale(inputs: ScalarInputs) -> float:
    quotes = vector(inputs, "quotes")
    cash, minimum, maximum = (number(inputs, key) for key in ("cash", "minimum", "maximum"))
    total = float(len(quotes) * minimum)
    target = min(cash, sum(maximum if q > 0 else minimum for q in quotes))
    if target <= total:
        return 0.0
    events: dict[float, list[float]] = {}
    for quote in quotes:
        if quote > 0:
            events.setdefault(minimum / quote, []).append(quote)
            events.setdefault(maximum / quote, []).append(-quote)
    previous = slope = 0.0
    for point, changes in sorted(events.items()):
        upper = total + (point - previous) * slope
        if slope > 0 and upper >= target:
            return previous + (target - total) / slope
        total, previous = upper, point
        slope = fsum((slope, *changes))
    return previous
