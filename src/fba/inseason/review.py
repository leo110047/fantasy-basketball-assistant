import numpy as np
from pydantic import JsonValue

from fba.contracts.base import DataError
from fba.contracts.config import Linear
from fba.contracts.inseason import InseasonLeague, InseasonParameters, LeagueSnapshot
from fba.contracts.inseason_results import (
    CalibrationBin,
    CalibrationHistory,
    CalibrationObservationRecord,
    PredictionRecord,
    WeeklyReview,
)
from fba.formulas.categories import category_values, score_samples
from fba.formulas.fitting import fit_shrinkage
from fba.formulas.registry import evaluate


def calibration_bins(
    predicted: tuple[float, ...], observed: tuple[float, ...], count: int
) -> tuple[CalibrationBin, ...]:
    rows: list[CalibrationBin] = []
    for index in range(count):
        members = tuple(
            (p, y)
            for p, y in zip(predicted, observed, strict=True)
            if min(count - 1, int(p * count)) == index
        )
        p = evaluate("mean", values=tuple(p for p, _ in members)).result if members else None
        y = evaluate("mean", values=tuple(y for _, y in members)).result if members else None
        rows.append(
            CalibrationBin(
                lower=index / count,
                upper=(index + 1) / count,
                count=len(members),
                predicted=p,
                observed=y,
                difference=abs(p - y) if p is not None and y is not None else None,
            )
        )
    return tuple(rows)


def weekly_review(
    league: InseasonLeague,
    params: InseasonParameters,
    snapshot: LeagueSnapshot,
    predictions: tuple[PredictionRecord, ...],
    week_id: str,
    adoption: dict[str, bool],
) -> WeeklyReview:
    records = tuple(p for p in predictions if p.week_id == week_id)
    if not records:
        raise DataError(f"review.{week_id}: no contemporaneously recorded forecasts")
    axes = (*league.base_stats, *(d.id for d in league.derived))
    scores = {s.team_id: s for s in snapshot.actual if s.week_id == week_id and s.final}
    cp: list[float] = []
    cy: list[float] = []
    wp: list[float] = []
    wy: list[float] = []
    adjusted: list[float] = []
    baseline: list[float] = []
    observed: list[float] = []
    rows: list[dict[str, JsonValue]] = []
    outcomes: list[dict[str, JsonValue]] = []
    for prediction in records:
        forecast = prediction.with_adjustments
        if forecast.home not in scores or forecast.away not in scores:
            raise DataError(f"review.{week_id}: final Yahoo scores are unavailable")
        needed = {
            t.stat_id
            for c in league.categories
            for t in (
                c.formula.terms
                if isinstance(c.formula, Linear)
                else (*c.formula.numerator, *c.formula.denominator)
            )
        }
        for tid in (forecast.home, forecast.away):
            if needed - scores[tid].totals.keys():
                raise DataError(f"review.{week_id}.{tid}: required category totals are missing")
        final = np.array(
            [
                [scores[tid].totals.get(s, 0.0) for s in axes]
                for tid in (forecast.home, forecast.away)
            ]
        )
        points, result = score_samples(
            final[:1],
            final[1:],
            league.categories,
            axes,
            league.scoring,
            league.category_ties,
            league.week_tie_value,
        )
        actual_values = category_values(final, league.categories, axes, directed=False)
        for i, category in enumerate(forecast.categories):
            cp.append(category.probability)
            cy.append(float(points[0, i]))
            adjusted.extend((category.home, category.away))
            base = prediction.without_adjustments.categories[i]
            baseline.extend((base.home, base.away))
            observed.extend((float(actual_values[0, i]), float(actual_values[1, i])))
            rows.append(
                {
                    "prediction": prediction.id,
                    "category": category.id,
                    "predicted": category.probability,
                    "observed": float(points[0, i]),
                    "home": float(actual_values[0, i]),
                    "away": float(actual_values[1, i]),
                }
            )
        if league.scoring == "h2h_one_win":
            wp.append(forecast.score)
            wy.append(float(result[0]))
        outcomes.extend(
            {
                "plan_id": plan.id,
                "adopted": adoption.get(plan.id),
                "predicted_gain": plan.delta_week,
                "actual_week_score": float(result[0]),
                "counterfactual_gain": None,
            }
            for plan in prediction.recommendations
        )
    category_brier = evaluate("brier", predicted=tuple(cp), observed=tuple(cy))
    week_brier = evaluate("brier", predicted=tuple(wp), observed=tuple(wy)) if wp else None
    with_error = evaluate("mae", predicted=tuple(adjusted), observed=tuple(observed))
    without_error = evaluate("mae", predicted=tuple(baseline), observed=tuple(observed))
    bins = calibration_bins(tuple(cp), tuple(cy), params.calibration_bins.value)
    return WeeklyReview(
        week_id=week_id,
        prediction_ids=tuple(p.id for p in records),
        rows=tuple(rows),
        category_brier=category_brier.result,
        week_brier=week_brier.result if week_brier else None,
        with_adjustments_mae=with_error.result,
        without_adjustments_mae=without_error.result,
        bins=bins,
        refit_alert=any(
            b.difference is not None and b.difference > params.calibration_alert.value for b in bins
        ),
        recommendation_outcomes=tuple(outcomes),
        traces=(category_brier, with_error, without_error, *((week_brier,) if week_brier else ())),
    )


def latest_reviews(reviews: tuple[WeeklyReview, ...]) -> tuple[WeeklyReview, ...]:
    # Snapshot history is append ordered. Score corrections append a new report;
    # retain every version on disk, count each week once in current metrics.
    latest = {review.week_id: review for review in reviews}
    return tuple(latest[week] for week in sorted(latest))


def cumulative_review(
    reviews: tuple[WeeklyReview, ...], params: InseasonParameters
) -> dict[str, JsonValue]:
    reviews = latest_reviews(reviews)
    if not reviews:
        return {"weeks": 0, "category_brier": None, "week_brier": None, "bins": []}
    predicted: list[float] = []
    observed: list[float] = []
    for review in latest_reviews(reviews):
        for row in review.rows:
            p, y = row["predicted"], row["observed"]
            if not isinstance(p, (int, float)) or not isinstance(y, (int, float)):
                raise DataError("review: stored probability or outcome is not numeric")
            predicted.append(float(p))
            observed.append(float(y))
    score = evaluate("brier", predicted=tuple(predicted), observed=tuple(observed))
    played = tuple(r for r in reviews if r.week_brier is not None)
    count = sum(len(r.prediction_ids) for r in played)
    weekly = (
        evaluate(
            "weighted_mean",
            values=tuple(r.week_brier for r in played if r.week_brier is not None),
            weights=tuple(float(len(r.prediction_ids)) for r in played),
        )
        if count
        else None
    )
    bins = calibration_bins(tuple(predicted), tuple(observed), params.calibration_bins.value)
    return {
        "weeks": len(reviews),
        "category_predictions": len(predicted),
        "category_brier": score.result,
        "week_brier": weekly.result if weekly else None,
        "traces": [
            trace.model_dump(mode="json") for trace in (score, *((weekly,) if weekly else ()))
        ],
        "bins": [r.model_dump(mode="json") for r in bins],
        "refit_alert": any(
            b.difference is not None and b.difference > params.calibration_alert.value for b in bins
        ),
    }


def calibration_history(
    season_id: str,
    predictions: tuple[PredictionRecord, ...],
    reviews: tuple[WeeklyReview, ...],
    snapshot_hashes: tuple[str, ...],
) -> CalibrationHistory:
    saved = {p.id: p for p in predictions}
    observations: dict[tuple[str, str], CalibrationObservationRecord] = {}
    for review in latest_reviews(reviews):
        for row in review.rows:
            identifier, category, outcome = row["prediction"], row["category"], row["observed"]
            if (
                not isinstance(identifier, str)
                or not isinstance(category, str)
                or not isinstance(outcome, (int, float))
            ):
                raise DataError("calibration history: malformed review row")
            if identifier not in saved:
                raise DataError("calibration history: original prediction is missing")
            original = saved[identifier]
            forecast = next(c for c in original.with_adjustments.categories if c.id == category)
            observations[identifier, category] = CalibrationObservationRecord(
                prediction_id=identifier,
                category=category,
                created_at=original.created_at,
                raw_probability=forecast.raw_probability,
                observed=float(outcome),
            )
    return CalibrationHistory(
        season_id=season_id,
        observations=tuple(observations[k] for k in sorted(observations)),
        snapshot_hashes=snapshot_hashes,
    )


def refit_history(history: CalibrationHistory, evidence_hash: str) -> dict[str, JsonValue]:
    predicted = tuple(o.raw_probability for o in history.observations)
    observed = tuple(o.observed for o in history.observations)
    c = fit_shrinkage(predicted, observed)
    calibrated = tuple(evaluate("calibration", p=p, c=c).result for p in predicted)
    return {
        "season_id": history.season_id,
        "calibration": {
            "value": c,
            "evidence": {
                "kind": "backtest",
                "reference": evidence_hash,
                "as_of": max(o.created_at for o in history.observations).isoformat(),
                "reason": "Fit from immutable prior-season forecasts and resolved outcomes; "
                "holdout not performed",
            },
        },
        "samples": len(predicted),
        "training_brier": evaluate("brier", predicted=calibrated, observed=observed).result,
        "holdout_passed": False,
    }
