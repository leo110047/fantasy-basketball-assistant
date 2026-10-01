from typing import Literal

import numpy as np
from scipy.optimize import linprog, root

from fba.contracts.base import DataError
from fba.contracts.data import Calibration, CalibrationPair
from fba.contracts.inseason import Proposal
from fba.core.proposals import latest_proposals
from fba.formulas.arrays import evaluate_array
from fba.formulas.registry import evaluate
from fba.formulas.vector import availability_regression


def fit_availability(
    pairs: tuple[CalibrationPair, ...],
    season_id: str,
    inputs: tuple[str, ...],
    method: Literal["ordinary_least_squares"],
) -> Calibration:
    ordered = sorted(pairs, key=lambda p: p.player_id)
    if len(ordered) < 2 or len({p.player_id for p in ordered}) != len(ordered):
        raise DataError("calibration: need at least two unique players")
    fitted = availability_regression(
        {
            "projected": np.array([p.projected_games for p in ordered]),
            "observed": np.array([p.actual_games for p in ordered]),
        }
    )
    return Calibration(
        training_season_id=season_id,
        method=method,
        intercept=float(fitted[0]),
        slope=float(fitted[1]),
        sample_size=len(ordered),
        inputs_sha256=tuple(sorted(inputs)),
    )


def fit_acceptance(
    proposals: tuple[Proposal, ...], minimum: int, tolerance: float
) -> tuple[float, float, float, float, tuple[float, ...], tuple[float, ...]]:
    rows = tuple(p for p in latest_proposals(proposals) if p.outcome in ("accepted", "rejected"))
    if len(rows) < minimum or len({p.outcome for p in rows}) < 2:
        raise DataError("acceptance.fit: insufficient resolved proposals or only one outcome class")
    x = np.array([[p.rank_delta, p.need_delta, -1.0] for p in rows])
    y = np.array([float(p.outcome == "accepted") for p in rows])
    scale = np.maximum(np.max(np.abs(x), axis=0), 1.0)
    x = x / scale
    if np.linalg.matrix_rank(x) < x.shape[1]:
        raise DataError("acceptance.fit: features do not identify all model coefficients")
    signed = x * (2 * y - 1)[:, None]
    # Complete or quasi separation has no finite unregularized MLE:
    # all signed margins >= 0, with at least one strictly positive.
    separation = linprog(
        np.zeros(3, dtype=np.float64),
        A_ub=np.vstack((-signed, -signed.sum(axis=0))),
        b_ub=np.append(np.zeros(len(y)), -1.0),
        bounds=(None, None),
        method="highs",
    )
    if separation.success:
        raise DataError("acceptance.fit: separated outcomes; finite coefficients cannot be fitted")
    if separation.status != 2:
        raise DataError("acceptance.fit: could not verify outcome overlap")

    def gradient(beta: np.ndarray[tuple[int], np.dtype[np.float64]]) -> np.ndarray:
        return evaluate_array("logistic_gradient", features=x, coefficients=beta, outcomes=y).result

    def hessian(beta: np.ndarray[tuple[int], np.dtype[np.float64]]) -> np.ndarray:
        return evaluate_array("logistic_hessian", features=x, coefficients=beta).result

    # For overlapping full-rank data the likelihood is strictly convex.
    # Solve its score equations directly: objective differences near the MLE
    # can round to zero before the requested gradient tolerance is reached.
    result = root(gradient, np.zeros(3), jac=hessian, method="hybr", tol=tolerance)
    residual = gradient(result.x)
    if (
        not np.isfinite(result.x).all()
        or not np.isfinite(residual).all()
        or np.max(np.abs(residual)) > tolerance
    ):
        raise DataError(f"acceptance.fit: optimizer failed ({result.message})")
    a, b, threshold = (float(v) for v in result.x / scale)
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
