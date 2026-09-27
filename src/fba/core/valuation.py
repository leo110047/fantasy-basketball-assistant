from math import fsum, sqrt

from fba.contracts.base import DataError
from fba.contracts.config import Category, LeagueRules, Linear, Term, ValuationParameters
from fba.contracts.projection import (
    CategoryNormalization,
    CategoryScore,
    PlayerValue,
    Projected,
    Valuation,
    ValuationRuler,
)


def validate_population(
    players: tuple[Projected, ...], axes: tuple[str, ...], categories: tuple[Category, ...]
) -> None:
    if (
        not players
        or len(set(axes)) != len(axes)
        or len({p.id for p in players}) != len(players)
        or any(len(p.stats) != len(axes) for p in players)
    ):
        raise DataError("valuation: missing population, duplicate IDs, or incompatible axes")
    for category in categories:
        formula = category.formula
        terms = (
            formula.terms
            if isinstance(formula, Linear)
            else formula.numerator + formula.denominator
        )
        if any(term.stat_id not in axes for term in terms):
            raise DataError(f"valuation.{category.id}: statistic absent from input axes")


def linear_value(
    terms: tuple[Term, ...], values: tuple[float, ...], axes: tuple[str, ...]
) -> float:
    return fsum(term.coefficient * values[axes.index(term.stat_id)] for term in terms)


def ratio_rate(
    players: tuple[Projected, ...], pool: tuple[int, ...], axes: tuple[str, ...], category: Category
) -> float | None:
    formula = category.formula
    if isinstance(formula, Linear):
        return None
    numerator = fsum(linear_value(formula.numerator, players[i].stats, axes) for i in pool)
    denominator = fsum(linear_value(formula.denominator, players[i].stats, axes) for i in pool)
    if denominator <= 0:
        raise DataError(f"valuation.{category.id}: population ratio denominator must be positive")
    return numerator / denominator


def category_impacts(
    players: tuple[Projected, ...], axes: tuple[str, ...], category: Category, rate: float | None
) -> tuple[float, ...]:
    formula = category.formula
    direction = 1 if category.direction == "higher" else -1
    if isinstance(formula, Linear):
        return tuple(direction * linear_value(formula.terms, p.stats, axes) for p in players)
    if rate is None:
        raise DataError(f"valuation.{category.id}: missing fitted ratio")
    return tuple(
        direction
        * (
            linear_value(formula.numerator, p.stats, axes)
            - rate * linear_value(formula.denominator, p.stats, axes)
        )
        for p in players
    )


def fit_category(
    players: tuple[Projected, ...], pool: tuple[int, ...], axes: tuple[str, ...], category: Category
) -> CategoryNormalization:
    ratio = ratio_rate(players, pool, axes, category)
    impacts = category_impacts(players, axes, category, ratio)
    mean = fsum(impacts[i] for i in pool) / len(pool)
    deviation = sqrt(fsum((impacts[i] - mean) ** 2 for i in pool) / len(pool))
    if deviation <= 0:
        raise DataError(f"valuation.{category.id}: degenerate category population")
    return CategoryNormalization(id=category.id, ratio=ratio, mean=mean, deviation=deviation)


def standardized_scores(
    players: tuple[Projected, ...],
    axes: tuple[str, ...],
    categories: tuple[Category, ...],
    normalizations: tuple[CategoryNormalization, ...],
) -> tuple[tuple[float, ...], ...]:
    validate_population(players, axes, categories)
    if tuple(c.id for c in categories) != tuple(n.id for n in normalizations):
        raise DataError("valuation: ruler category axes disagree")
    if any(n.deviation <= 0 for n in normalizations):
        raise DataError("valuation: ruler deviation must be positive")
    return tuple(
        tuple((v - n.mean) / n.deviation for v in category_impacts(players, axes, c, n.ratio))
        for c, n in zip(categories, normalizations, strict=True)
    )


def fit_ruler(
    players: tuple[Projected, ...],
    axes: tuple[str, ...],
    league: LeagueRules,
    parameters: ValuationParameters,
) -> ValuationRuler:
    validate_population(players, axes, league.categories)
    count = league.teams * (len(league.starter_slots) + league.bench_slots)
    indices = tuple(range(len(players)))

    def rank(scores: tuple[float, ...], candidates: tuple[int, ...]) -> tuple[int, ...]:
        return tuple(sorted(candidates, key=lambda i: (-scores[i], players[i].id)))

    pool = rank(tuple(p.minutes for p in players), indices)[:count]
    if len(pool) != count:
        raise DataError("valuation: insufficient draft population")
    scales: tuple[CategoryNormalization, ...] = ()
    scores: tuple[float, ...] = ()
    for _ in range(parameters.pool_iterations):
        scales = tuple(fit_category(players, pool, axes, cat) for cat in league.categories)
        z = standardized_scores(players, axes, league.categories, scales)
        scores = tuple(fsum(column[i] for column in z) for i in indices)
        pool = rank(scores, indices)[:count]
    healthy = tuple(
        i for i, p in enumerate(players) if p.expected_games >= parameters.healthy_games
    )
    replacement = rank(scores, healthy)[count : count + parameters.replacement_count]
    if len(replacement) != parameters.replacement_count:
        raise DataError("valuation: insufficient healthy replacement population")
    baseline = fsum(scores[i] for i in replacement) / len(replacement)
    return ValuationRuler(replacement_score=baseline, categories=scales)


def auction_dollars(
    utilities: tuple[float, ...], ids: tuple[str, ...], league: LeagueRules
) -> tuple[float, ...]:
    count = league.teams * (len(league.starter_slots) + league.bench_slots)
    positive = tuple(max(u, 0) for u in utilities)
    top = sorted(range(len(ids)), key=lambda i: (-positive[i], ids[i]))[:count]
    if len(top) != count or any(positive[i] <= 0 for i in top):
        raise DataError("valuation: insufficient positive auction population")
    scale = (league.teams * league.budget - count * league.minimum_bid) / fsum(
        positive[i] for i in top
    )
    return tuple(league.minimum_bid + p * scale if p > 0 else 0 for p in positive)


def value(
    projections: tuple[Projected, ...],
    catalog_ids: tuple[str, ...],
    axes: tuple[str, ...],
    league: LeagueRules,
    parameters: ValuationParameters,
    season_games: int,
    ruler: ValuationRuler,
) -> Valuation:
    players = tuple(sorted(projections, key=lambda p: p.id))
    if len(set(catalog_ids)) != len(catalog_ids) or season_games <= 0:
        raise DataError("valuation: duplicate catalog IDs or invalid season games")
    z = standardized_scores(players, axes, league.categories, ruler.categories)
    utility = tuple(
        (fsum(col[i] for col in z) - ruler.replacement_score) * p.expected_games / season_games
        for i, p in enumerate(players)
    )
    prices = auction_dollars(utility, tuple(p.id for p in players), league)
    by_id = {
        p.id: PlayerValue(
            id=p.id,
            fair=round(prices[i], parameters.result_decimals),
            utility=round(utility[i], parameters.result_decimals),
            categories=tuple(
                CategoryScore(id=c.id, z=round(column[i], parameters.result_decimals))
                for c, column in zip(league.categories, z, strict=True)
            ),
            unavailable_reason=None,
        )
        for i, p in enumerate(players)
    }
    return Valuation(
        replacement_score=round(ruler.replacement_score, parameters.result_decimals),
        players=tuple(
            by_id[i]
            if i in by_id
            else PlayerValue(
                id=i,
                fair=None,
                utility=None,
                categories=(),
                unavailable_reason="no active-team projection",
            )
            for i in sorted(catalog_ids)
        ),
    )
