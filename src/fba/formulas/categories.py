import numpy as np
from numpy.typing import NDArray

from fba.contracts.base import DataError
from fba.contracts.config import Category, Linear, Term
from fba.contracts.inseason import DerivedStat
from fba.formulas.simulation import (
    array_product,
    category_ratio,
    comparison_margin,
    derived_sum,
    game_threshold,
    linear_totals,
    nonnegative_samples,
)
from fba.formulas.vector import category_points, week_points

type Array = NDArray[np.float64]


def total_terms(box: Array, terms: tuple[Term, ...], axes: tuple[str, ...]) -> Array:
    return linear_totals(
        {
            "values": box,
            "indices": np.asarray([axes.index(t.stat_id) for t in terms], dtype=np.float64),
            "weights": np.asarray([t.coefficient for t in terms]),
        }
    )


def category_values(
    box: Array, categories: tuple[Category, ...], axes: tuple[str, ...], *, directed: bool = True
) -> Array:
    positive = nonnegative_samples({"values": box})
    result: list[Array] = []
    for category in categories:
        formula = category.formula
        numerator = total_terms(
            positive, formula.terms if isinstance(formula, Linear) else formula.numerator, axes
        )
        value = numerator
        if not isinstance(formula, Linear):
            denominator = total_terms(positive, formula.denominator, axes)
            if formula.zero_denominator == "error" and np.any(denominator <= 0):
                raise DataError(f"category.{category.id}: zero denominator")
            value = category_ratio(
                {
                    "numerator": numerator,
                    "denominator": denominator,
                    "zero_value": numerator
                    if formula.zero_denominator == "numerator"
                    else np.zeros_like(numerator),
                }
            )
        result.append(
            array_product(
                {
                    "values": value,
                    "multiplier": np.asarray(
                        -1 if directed and category.direction == "lower" else 1
                    ),
                }
            )
        )
    return np.stack(result, axis=-1)


def derive_games(box: Array, base: tuple[str, ...], definitions: tuple[DerivedStat, ...]) -> Array:
    axes, columns = list(base), [box[..., i] for i in range(len(base))]
    for definition in definitions:
        values = array_product(
            {
                "values": np.stack(
                    [columns[axes.index(t.stat_id)] for t in definition.terms], axis=-1
                ),
                "multiplier": np.asarray([t.coefficient for t in definition.terms]),
            }
        )
        if definition.kind == "linear":
            value = derived_sum({"values": values})
        else:
            value = game_threshold(
                {
                    "values": values,
                    "threshold": np.asarray(definition.threshold),
                    "minimum_hits": np.asarray(definition.minimum_hits),
                }
            )
        axes.append(definition.id)
        columns.append(value)
    return np.stack(columns, axis=-1)


def score_samples(
    home: Array,
    away: Array,
    categories: tuple[Category, ...],
    axes: tuple[str, ...],
    mode: str,
    category_ties: str,
    week_tie: float,
) -> tuple[Array, Array]:
    differences = scoring_differences(home, away, categories, axes)
    ties = np.array([c.tie_value if category_ties == "use_tie_value" else 0.0 for c in categories])
    inputs = {"differences": differences, "ties": ties}
    points = category_points(inputs)
    if mode == "h2h_each_category":
        return points, points.sum(axis=-1)
    return points, week_points({**inputs, "week_tie": np.asarray(week_tie)})


def scoring_differences(
    home: Array, away: Array, categories: tuple[Category, ...], axes: tuple[str, ...]
) -> Array:
    a, b = category_values(home, categories, axes), category_values(away, categories, axes)
    return np.stack(
        [
            comparison_margin(
                {
                    "home": a[..., i],
                    "away": b[..., i],
                    "decimals": np.asarray(c.comparison_decimals),
                }
            )
            for i, c in enumerate(categories)
        ],
        axis=-1,
    )


def sample_scores(
    home: Array,
    away: Array,
    categories: tuple[Category, ...],
    axes: tuple[str, ...],
    mode: str,
    category_ties: str,
    week_tie: float,
) -> Array:
    if mode == "h2h_each_category":
        return score_samples(home, away, categories, axes, mode, category_ties, week_tie)[1]
    differences = scoring_differences(home, away, categories, axes)
    return week_points(
        {
            "differences": differences,
            "ties": np.zeros(len(categories)),
            "week_tie": np.asarray(week_tie),
        }
    )
