from math import fsum, sqrt

from fba.contracts.base import DataError
from fba.contracts.config import CalculationModel, ResourceModel
from fba.contracts.projection import (
    EvaluationInput,
    EvaluationMetrics,
    EvaluationResult,
    Projected,
    ValuationRuler,
)
from fba.core.valuation import auction_dollars, fit_ruler, standardized_scores


def ranks(values: tuple[float, ...]) -> tuple[float, ...]:
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
    return tuple(result)


def correlation(x: tuple[float, ...], y: tuple[float, ...]) -> float:
    if len(x) != len(y) or len(x) < 2:
        raise DataError("evaluation.common_ids: at least two paired observations required")
    a, b = ranks(x), ranks(y)
    ma, mb = fsum(a) / len(a), fsum(b) / len(b)
    denominator = sqrt(fsum((v - ma) ** 2 for v in a) * fsum((v - mb) ** 2 for v in b))
    if denominator == 0:
        raise DataError("evaluation: rank correlation undefined for constant values")
    return fsum((v - ma) * (w - mb) for v, w in zip(a, b, strict=True)) / denominator


def median(values: tuple[float, ...]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def season_utilities(
    players: tuple[Projected, ...], inputs: EvaluationInput, ruler: ValuationRuler
) -> tuple[float, ...]:
    z = standardized_scores(
        players, inputs.stat_ids, inputs.config.league.categories, ruler.categories
    )
    return tuple(
        (fsum(column[i] for column in z) - ruler.replacement_score)
        * p.expected_games
        / inputs.season_games
        for i, p in enumerate(players)
    )


def compare(
    predicted: tuple[Projected, ...], inputs: EvaluationInput, ruler: ValuationRuler, label: str
) -> EvaluationMetrics:
    model = inputs.config.model
    if not isinstance(model, (CalculationModel, ResourceModel)):
        raise DataError("evaluation: calculation model required")
    pred = {p.id: p for p in predicted}
    actual = {p.id: p for p in inputs.actual}
    if len(pred) != len(predicted) or len(actual) != len(inputs.actual):
        raise DataError("evaluation: duplicate player IDs")
    if not set(inputs.common_ids) <= set(pred) or len(set(inputs.common_ids)) != len(
        inputs.common_ids
    ):
        raise DataError("evaluation.common_ids: duplicate or absent prediction")
    ids = tuple(sorted(set(pred) | set(actual)))
    pvalues = dict(
        zip((p.id for p in predicted), season_utilities(predicted, inputs, ruler), strict=True)
    )
    avalues = dict(
        zip(
            (p.id for p in inputs.actual),
            season_utilities(inputs.actual, inputs, ruler),
            strict=True,
        )
    )
    # This reference evaluator explicitly assigns zero production to absent season records.
    pv = tuple(pvalues.get(i, 0) for i in ids)
    av = tuple(avalues.get(i, 0) for i in ids)
    count = inputs.config.league.teams * (
        len(inputs.config.league.starter_slots) + inputs.config.league.bench_slots
    )
    top_pred = set(sorted(range(len(ids)), key=lambda i: (-pv[i], ids[i]))[:count])
    top_actual = set(sorted(range(len(ids)), key=lambda i: (-av[i], ids[i]))[:count])
    pd = auction_dollars(pv, ids, inputs.config.league)
    ad = auction_dollars(av, ids, inputs.config.league)
    error = tuple(abs(pd[i] - ad[i]) for i in sorted(top_pred | top_actual))
    score = correlation(
        tuple(pvalues[i] for i in inputs.common_ids),
        tuple(avalues.get(i, 0) for i in inputs.common_ids),
    )
    games = fsum(
        abs(pred[i].expected_games - (actual[i].expected_games if i in actual else 0))
        for i in inputs.common_ids
    ) / len(inputs.common_ids)
    return EvaluationMetrics(
        id=label,
        predicted_players=len(predicted),
        common_players=len(inputs.common_ids),
        top_draft_hits=len(top_pred & top_actual),
        rank_correlation=round(score, model.valuation.result_decimals),
        median_dollar_error=round(median(error), model.valuation.result_decimals),
        games_mae=round(games, model.valuation.result_decimals),
    )


def evaluate(inputs: EvaluationInput, input_sha256: str) -> EvaluationResult:
    if not isinstance(inputs.config.model, (CalculationModel, ResourceModel)):
        raise DataError("evaluation: calculation model required")
    if not inputs.predictions or len({p.id for p in inputs.predictions}) != len(inputs.predictions):
        raise DataError("evaluation: missing or duplicate variants")
    ruler = fit_ruler(
        tuple(sorted(inputs.actual, key=lambda p: p.id)),
        inputs.stat_ids,
        inputs.config.league,
        inputs.config.model.valuation,
    )
    return EvaluationResult(
        format_version=1,
        algorithm="fixed-outcome-ruler-v1",
        input_sha256=input_sha256,
        config=inputs.config.refs,
        ruler=ruler,
        variants=tuple(
            compare(tuple(sorted(p.players, key=lambda row: row.id)), inputs, ruler, p.id)
            for p in sorted(inputs.predictions, key=lambda p: p.id)
        ),
    )
