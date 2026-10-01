import numpy as np
from numpy.typing import NDArray

from fba.contracts.base import DataError
from fba.contracts.config import Category, Linear, Term
from fba.contracts.inseason import DerivedStat
from fba.formulas.vector import category_points, week_points

type Array = NDArray[np.float64]


def total_terms(box: Array, terms: tuple[Term, ...], axes: tuple[str, ...]) -> Array:
    if len(terms) == 1:
        term = terms[0]
        return np.asarray(box[..., axes.index(term.stat_id)] * term.coefficient + 0.0)
    return sum(
        (box[..., axes.index(t.stat_id)] * t.coefficient for t in terms),
        start=np.zeros(box.shape[:-1]),
    )


def category_values(
    box: Array, categories: tuple[Category, ...], axes: tuple[str, ...], *, directed: bool = True
) -> Array:
    positive = np.maximum(box, 0)
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
            value = np.divide(
                numerator,
                denominator,
                out=numerator.copy()
                if formula.zero_denominator == "numerator"
                else np.zeros_like(numerator),
                where=denominator > 0,
            )
        result.append(value * (-1 if directed and category.direction == "lower" else 1))
    return np.stack(result, axis=-1)


def derive_games(box: Array, base: tuple[str, ...], definitions: tuple[DerivedStat, ...]) -> Array:
    axes, columns = list(base), [box[..., i] for i in range(len(base))]
    for definition in definitions:
        values = np.stack(
            [columns[axes.index(t.stat_id)] * t.coefficient for t in definition.terms], axis=-1
        )
        if definition.kind == "linear":
            value = values.sum(axis=-1)
        else:
            value = (
                (values >= definition.threshold).sum(axis=-1) >= definition.minimum_hits
            ).astype(np.float64)
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
            np.round(a[..., i], c.comparison_decimals) - np.round(b[..., i], c.comparison_decimals)
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
