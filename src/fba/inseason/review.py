from collections import Counter

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
from fba.data.codec import canonical, digest
from fba.formulas.categories import category_values, score_samples
from fba.formulas.fitting import fit_shrinkage
from fba.formulas.registry import evaluate


def calibration_bins(
    predicted: tuple[float, ...],
    observed: tuple[float, ...],
    count: int,
    *,
    clusters: tuple[str, ...] | None = None,
    params: InseasonParameters | None = None,
) -> tuple[CalibrationBin, ...]:
    if clusters is not None and len(clusters) != len(predicted):
        raise DataError("calibration.clusters: observation count differs")
    rows: list[CalibrationBin] = []
    for index in range(count):
        members = tuple(
            (p, y)
            for p, y in zip(predicted, observed, strict=True)
            if min(count - 1, int(p * count)) == index
        )
        predicted_mean = evaluate("mean", values=tuple(p for p, _ in members)) if members else None
        observed_mean = evaluate("mean", values=tuple(y for _, y in members)) if members else None
        p = predicted_mean.result if predicted_mean else None
        y = observed_mean.result if observed_mean else None
        weights = Counter(
            cluster
            for probability, cluster in zip(predicted, clusters or (), strict=False)
            if min(count - 1, int(probability * count)) == index
        )
        n = len(weights) if clusters is not None else len(members)
        effective = (
            evaluate("effective_samples", weights=tuple(float(w) for w in weights.values()))
            if weights
            else None
        )
        effective_n = effective.result if effective else float(n)
        # Fully correlated categories within a week form one bounded cluster.
        # Unequal cluster weights reduce the effective independent sample count.
        margin = (
            evaluate("monitor_margin", samples=effective_n, z=params.calibration_confidence_z.value)
            if effective_n and params
            else None
        )
        uncertainty = margin.result if margin else None
        error = (
            evaluate("absolute_error", predicted=p, observed=y)
            if p is not None and y is not None
            else None
        )
        difference = error.result if error else None
        rows.append(
            CalibrationBin(
                lower=index / count,
                upper=(index + 1) / count,
                count=len(members),
                predicted=p,
                observed=y,
                difference=difference,
                independent_samples=n,
                effective_samples=effective_n,
                uncertainty=uncertainty,
                traces=(
                    *((predicted_mean, observed_mean) if predicted_mean and observed_mean else ()),
                    *((effective,) if effective else ()),
                    *((margin,) if margin else ()),
                    *((error,) if error else ()),
                ),
                alert=params is not None
                and effective_n >= params.calibration_minimum.value
                and difference is not None
                and uncertainty is not None
                and difference > params.calibration_alert.value + uncertainty,
            )
        )
    return tuple(rows)


def evaluation_cohort(predictions: tuple[PredictionRecord, ...]) -> tuple[PredictionRecord, ...]:
    """One fixed origin per matchup-week and scoring semantic; keep raw history."""
    origins: dict[tuple[str, str, str, str], PredictionRecord] = {}
    for prediction in sorted(predictions, key=lambda p: (p.created_at, p.id)):
        f = prediction.with_adjustments
        key = prediction.week_id, f.home, f.away, prediction.week_score_kind
        origins.setdefault(key, prediction)
    return tuple(origins[k] for k in sorted(origins))


def validate_review_rules(
    league: InseasonLeague, predictions: tuple[PredictionRecord, ...], week_id: str
) -> None:
    """Check every archived decision before applying today's scoring rules."""
    rule_hash = digest(canonical(league))
    category_ids = tuple(c.id for c in league.categories)
    for prediction in predictions:
        if (
            tuple(c.id for c in prediction.with_adjustments.categories) != category_ids
            or tuple(c.id for c in prediction.without_adjustments.categories) != category_ids
        ):
            raise DataError(f"review.{week_id}: archived category axes differ from current rules")
        if prediction.league_sha256 is not None and prediction.league_sha256 != rule_hash:
            raise DataError(
                f"review.{week_id}: archived league rules differ from current rules; "
                "restore the recorded rules before evaluating this history"
            )


def weekly_review(
    league: InseasonLeague,
    params: InseasonParameters,
    snapshot: LeagueSnapshot,
    predictions: tuple[PredictionRecord, ...],
    week_id: str,
    adoption: dict[str, bool],
) -> WeeklyReview:
    all_records = tuple(p for p in predictions if p.week_id == week_id)
    records = evaluation_cohort(all_records)
    if not records:
        raise DataError(f"review.{week_id}: no contemporaneously recorded forecasts")
    validate_review_rules(league, all_records, week_id)
    week_score_kind = (
        "win_probability"
        if any(p.week_score_kind == "win_probability" for p in records)
        else "standings_points"
    )
    week_prediction_ids: list[str] = []
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
    week_observations: list[dict[str, JsonValue]] = []
    category_origins: dict[tuple[str, str], str] = {}
    for record in sorted(records, key=lambda p: (p.created_at, p.id)):
        f = record.with_adjustments
        category_origins.setdefault((f.home, f.away), record.id)
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
            league.week_tie_value if prediction.week_score_kind == "standings_points" else 0.0,
        )
        actual_values = category_values(final, league.categories, axes, directed=False)
        for i, category in enumerate(forecast.categories):
            if category_origins[forecast.home, forecast.away] != prediction.id:
                continue
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
        if league.scoring == "h2h_one_win" and prediction.week_score_kind == week_score_kind:
            wp.append(forecast.score)
            wy.append(float(result[0]))
            week_prediction_ids.append(prediction.id)
            week_observations.append(
                {
                    "prediction": prediction.id,
                    "predicted": forecast.score,
                    "raw_probability": forecast.raw_score,
                    "observed": float(result[0]),
                }
            )
    # Metrics use a fixed origin; decision audit includes all distinct plans.
    audited: set[str] = set()
    for prediction in all_records:
        forecast = prediction.with_adjustments
        if forecast.home not in scores or forecast.away not in scores:
            raise DataError(f"review.{week_id}: final Yahoo scores are unavailable")
        final = np.array(
            [
                [scores[tid].totals.get(s, 0.0) for s in axes]
                for tid in (forecast.home, forecast.away)
            ]
        )
        _, result = score_samples(
            final[:1], final[1:], league.categories, axes, league.scoring, league.category_ties, 0.0
        )
        for plan in prediction.recommendations:
            if plan.id in audited:
                continue
            audited.add(plan.id)
            outcomes.append(
                {
                    "plan_id": plan.id,
                    "adopted": adoption.get(plan.id),
                    "predicted_gain": plan.delta_week,
                    "actual_week_score": float(result[0]),
                    "counterfactual_gain": None,
                }
            )
    category_brier = evaluate("brier", predicted=tuple(cp), observed=tuple(cy))
    week_brier = evaluate("brier", predicted=tuple(wp), observed=tuple(wy)) if wp else None
    with_error = evaluate("mae", predicted=tuple(adjusted), observed=tuple(observed))
    without_error = evaluate("mae", predicted=tuple(baseline), observed=tuple(observed))
    bins = calibration_bins(
        tuple(cp),
        tuple(cy),
        params.calibration_bins.value,
        clusters=tuple(week_id for _ in cp),
        params=params,
    )
    return WeeklyReview(
        rules_verified=all(p.league_sha256 is not None for p in all_records),
        week_id=week_id,
        prediction_ids=tuple(p.id for p in records),
        rows=tuple(rows),
        category_brier=category_brier.result,
        week_brier=week_brier.result if week_brier else None,
        week_score_kind=week_score_kind,
        week_prediction_ids=tuple(week_prediction_ids),
        with_adjustments_mae=with_error.result,
        without_adjustments_mae=without_error.result,
        bins=bins,
        refit_alert=any(b.alert for b in bins),
        week_observations=tuple(week_observations),
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
    played = tuple(
        r for r in reviews if r.week_brier is not None and r.week_score_kind == "win_probability"
    )
    count = sum(len(r.week_prediction_ids) for r in played)
    weekly = (
        evaluate(
            "weighted_mean",
            values=tuple(r.week_brier for r in played if r.week_brier is not None),
            weights=tuple(float(len(r.week_prediction_ids)) for r in played),
        )
        if count
        else None
    )
    clusters = tuple(r.week_id for r in reviews for _ in r.rows)
    bins = calibration_bins(
        tuple(predicted),
        tuple(observed),
        params.calibration_bins.value,
        clusters=clusters,
        params=params,
    )
    return {
        "weeks": len(reviews),
        "unverified_rule_weeks": sum(not r.rules_verified for r in reviews),
        "category_predictions": len(predicted),
        "category_brier": score.result,
        "week_brier": weekly.result if weekly else None,
        "excluded_legacy_weeks": sum(
            r.week_brier is not None and r.week_score_kind == "standings_points" for r in reviews
        ),
        "traces": [
            trace.model_dump(mode="json") for trace in (score, *((weekly,) if weekly else ()))
        ],
        "bins": [r.model_dump(mode="json") for r in bins],
        "refit_alert": any(b.alert for b in bins),
    }


def calibration_history(
    season_id: str,
    predictions: tuple[PredictionRecord, ...],
    reviews: tuple[WeeklyReview, ...],
    snapshot_hashes: tuple[str, ...],
) -> CalibrationHistory:
    saved = {p.id: p for p in predictions}
    observations: dict[tuple[str, str], CalibrationObservationRecord] = {}
    weeks: dict[str, CalibrationObservationRecord] = {}
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
                league_sha256=original.league_sha256 if review.rules_verified else None,
                prediction_id=identifier,
                category=category,
                created_at=original.created_at,
                raw_probability=forecast.raw_probability,
                observed=float(outcome),
            )
        if review.week_score_kind == "win_probability":
            for row in review.week_observations:
                identifier, outcome = row["prediction"], row["observed"]
                if not isinstance(identifier, str) or not isinstance(outcome, (int, float)):
                    raise DataError("calibration history: malformed weekly outcome")
                original = saved[identifier]
                if original.week_score_kind == "win_probability":
                    weeks[identifier] = CalibrationObservationRecord(
                        league_sha256=original.league_sha256 if review.rules_verified else None,
                        prediction_id=identifier,
                        category="week",
                        created_at=original.created_at,
                        raw_probability=original.with_adjustments.raw_score,
                        observed=float(outcome),
                    )
    return CalibrationHistory(
        season_id=season_id,
        observations=tuple(observations[k] for k in sorted(observations)),
        snapshot_hashes=snapshot_hashes,
        week_observations=tuple(weeks[k] for k in sorted(weeks)),
    )


def fit_observations(
    observations: tuple[CalibrationObservationRecord, ...], evidence_hash: str
) -> dict[str, JsonValue] | None:
    if not observations:
        return None
    predicted = tuple(o.raw_probability for o in observations)
    observed = tuple(o.observed for o in observations)
    rules_verified = all(o.league_sha256 is not None for o in observations)
    c = fit_shrinkage(predicted, observed)
    calibrated = tuple(evaluate("calibration", p=p, c=c).result for p in predicted)
    return {
        "value": c,
        "evidence": {
            "kind": "backtest",
            "reference": evidence_hash,
            "as_of": max(o.created_at for o in observations).isoformat(),
            "reason": (
                "Fit from immutable fixed-origin prior-season forecasts and outcomes; "
                "holdout not performed"
                + ("; historical league rules unverified" if not rules_verified else "")
            ),
        },
        "rules_verified": rules_verified,
        "samples": len(predicted),
        "training_brier": evaluate("brier", predicted=calibrated, observed=observed).result,
    }


def refit_history(history: CalibrationHistory, evidence_hash: str) -> dict[str, JsonValue]:
    category = fit_observations(history.observations, evidence_hash)
    if category is None:
        raise DataError("calibration history: no resolved category observations")
    return {
        "season_id": history.season_id,
        "calibration": category,
        "week_calibration": fit_observations(history.week_observations, evidence_hash),
        "samples": len(history.observations),
        "training_brier": category["training_brier"],
        "holdout_passed": False,
    }
