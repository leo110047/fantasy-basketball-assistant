from fba.contracts.auction import FitCategory
from fba.contracts.config import FitParameters
from fba.contracts.formula import ArrayFormulaTrace, FormulaTrace
from fba.formulas.arrays import Array, evaluate_array
from fba.formulas.registry import evaluate


def matrix_root(covariance: Array) -> Array:
    return evaluate_array("covariance_root", covariance=covariance).result


def margin_score(difference: Array, bandwidth: float) -> Array:
    return evaluate_array("soft_majority", differences=difference, bandwidth=bandwidth).result


def category_diagnostics(
    difference: Array, ids: tuple[str, ...], parameters: FitParameters
) -> tuple[FitCategory, ...]:
    result = evaluate_array(
        "category_marginals",
        differences=difference,
        bandwidth=parameters.bandwidth,
        step=parameters.gradient_fraction,
    )
    return tuple(
        FitCategory(id=cid, lead_share=float(row[0]), marginal_weight=float(row[1]))
        for cid, row in zip(ids, result.result, strict=True)
    )


def diagnostic_traces(
    difference: Array, parameters: FitParameters
) -> tuple[FormulaTrace | ArrayFormulaTrace, ...]:
    score = evaluate_array("soft_majority", differences=difference, bandwidth=parameters.bandwidth)
    weights = evaluate_array(
        "category_marginals",
        differences=difference,
        bandwidth=parameters.bandwidth,
        step=parameters.gradient_fraction,
    )
    average = evaluate("mean", values=tuple(float(v) for v in score.result))
    return score.trace, weights.trace, average
