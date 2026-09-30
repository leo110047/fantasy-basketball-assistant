import numpy as np
from scipy.optimize import minimize

from fba.contracts.base import DataError
from fba.contracts.inseason import Proposal
from fba.core.proposals import latest_proposals
from fba.formulas.arrays import evaluate_array
from fba.formulas.registry import evaluate


def fit_acceptance(
    proposals: tuple[Proposal, ...], minimum: int, tolerance: float
) -> tuple[float, float, float, float, tuple[float, ...], tuple[float, ...]]:
    rows = tuple(p for p in latest_proposals(proposals) if p.outcome in ("accepted", "rejected"))
    if len(rows) < minimum or len({p.outcome for p in rows}) < 2:
        raise DataError("acceptance.fit: insufficient resolved proposals or only one outcome class")
    x = np.array([[p.rank_delta, p.need_delta, -1.0] for p in rows])
    y = np.array([float(p.outcome == "accepted") for p in rows])

    def loss(beta: np.ndarray[tuple[int], np.dtype[np.float64]]) -> float:
        return float(
            evaluate_array("logistic_objective", features=x, coefficients=beta, outcomes=y).result
        )

    result = minimize(loss, np.zeros(3), method="BFGS", tol=tolerance)
    if not result.success or not np.isfinite(result.x).all():
        raise DataError(f"acceptance.fit: optimizer failed ({result.message})")
    a, b, threshold = (float(v) for v in result.x)
    predicted = tuple(
        evaluate(
            "acceptance",
            beta_rank=a,
            beta_need=b,
            threshold=threshold,
            noise=1.0,
            delta_rank=p.rank_delta,
            delta_need=p.need_delta,
        ).result
        for p in rows
    )
    actual = tuple(float(v) for v in y)
    logloss = evaluate("log_loss", predicted=predicted, observed=actual, epsilon=tolerance).result
    return a, b, threshold, logloss, predicted, actual


def fit_shrinkage(predicted: tuple[float, ...], observed: tuple[float, ...]) -> float:
    return float(
        evaluate_array(
            "calibration_fit", predicted=np.array(predicted), observed=np.array(observed)
        ).result
    )
