from datetime import timedelta
from itertools import product
from math import fsum
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from fba.contracts.base import DataError
from fba.contracts.config import Evidence
from fba.contracts.inseason import InseasonParameters, NumberParameter
from fba.contracts.inseason_backtest import BacktestReport, BacktestStudy, ProjectionStudySeason
from fba.data.codec import canonical, digest
from fba.formulas.fitting import fit_shrinkage
from fba.formulas.registry import evaluate
from fba.inseason.parameter_fit import availability_fit, availability_samples, production_trial
from fba.inseason.projection import observed_boxes
from fba.inseason.review import calibration_bins


def metric(row: dict[str, JsonValue], key: str) -> float:
    value = row[key]
    if not isinstance(value, (int, float)):
        raise DataError(f"backtest.{key}: expected numeric metric")
    return float(value)


def checkpoint_errors(
    season: ProjectionStudySeason, k: dict[str, float], checkpoints: tuple[int, ...]
) -> tuple[dict[str, JsonValue], ...]:
    boxes = observed_boxes(season.players, season.players.as_of + timedelta(microseconds=1))
    priors = {p.player_id: p for p in season.priors.players}
    zone = ZoneInfo(season.league.timezone)
    rows: list[dict[str, JsonValue]] = []
    for checkpoint in checkpoints:
        for stat in season.league.base_stats:
            errors: dict[str, list[float]] = {"blend": [], "prior": [], "current": []}
            for pid, history in boxes.items():
                current = tuple(
                    b
                    for b in history
                    if season.league.starts_on
                    <= b.played_at.astimezone(zone).date()
                    <= season.league.ends_on
                )
                if len(current) <= checkpoint or pid not in priors:
                    continue
                past, future = current[:checkpoint], current[checkpoint:]
                minutes = fsum(b.minutes for b in past)
                remaining = fsum(b.minutes for b in future)
                if minutes == 0 or remaining == 0:
                    continue
                total = fsum(b.stats[stat] for b in past)
                actual = evaluate(
                    "ratio",
                    numerator=fsum(b.stats[stat] for b in future),
                    denominator=remaining,
                    zero_value=0.0,
                ).result
                prior = priors[pid].rates[stat]
                mixed = evaluate(
                    "blend", k=k[stat], prior=prior, total=total, sample=minutes
                ).result
                for name, value in (
                    ("blend", mixed),
                    ("prior", prior),
                    (
                        "current",
                        evaluate(
                            "ratio", numerator=total, denominator=minutes, zero_value=0.0
                        ).result,
                    ),
                ):
                    errors[name].append(abs(value - actual))
            if not errors["blend"]:
                raise DataError(
                    f"backtest.{season.league.season_id}.{checkpoint}.{stat}: "
                    "no holdout observations"
                )
            rows.append(
                {
                    "checkpoint": checkpoint,
                    "stat": stat,
                    "players": len(errors["blend"]),
                    **{
                        name: evaluate("mean", values=tuple(values)).result
                        for name, values in errors.items()
                    },
                }
            )
    return tuple(rows)


def run_study(
    study: BacktestStudy, params: InseasonParameters, input_sha256: str, parameters_sha256: str
) -> BacktestReport:
    if parameters_sha256 != digest(canonical(params)):
        raise DataError("backtest.parameters_sha256: input parameter hash mismatch")
    if study.training.league.ends_on >= study.validation.league.starts_on:
        raise DataError("backtest: training season must end before validation season begins")
    candidates_values = (
        *study.k_candidates,
        *study.minute_k_candidates,
        *study.half_life_candidates,
        *study.shot_k_candidates,
        *study.production_sigma_candidates,
    )
    if (
        not study.k_candidates
        or not study.checkpoints
        or not study.minute_k_candidates
        or not study.half_life_candidates
        or not study.shot_k_candidates
        or not study.production_sigma_candidates
        or any(k <= 0 for k in candidates_values)
        or any(n <= 0 for n in study.checkpoints)
    ):
        raise DataError("backtest: positive candidate k values and checkpoints required")
    if not study.training_outcomes or not study.validation_outcomes:
        raise DataError(
            "backtest: independent training and validation matchup observations required"
        )
    candidates: dict[float, tuple[dict[str, JsonValue], ...]] = {}
    for k in study.k_candidates:
        candidates[k] = checkpoint_errors(
            study.training, dict.fromkeys(study.training.league.base_stats, k), study.checkpoints
        )
    fitted = {
        stat: min(
            study.k_candidates,
            key=lambda k: (
                fsum(metric(row, "blend") for row in candidates[k] if row["stat"] == stat),
                k,
            ),
        )
        for stat in study.training.league.base_stats
    }
    rows = checkpoint_errors(study.validation, fitted, study.checkpoints)
    minutes = min(
        product(study.minute_k_candidates, study.half_life_candidates),
        key=lambda pair: component_error(
            study.training, study.checkpoints, "minutes", pair[0], pair[1]
        ),
    )
    shots = {
        shot.id: min(
            study.shot_k_candidates,
            key=lambda k: component_error(
                study.training, study.checkpoints, shot.id, k, minutes[1]
            ),
        )
        for shot in study.training.league.shots
    }
    components = (("minutes", minutes[0]), *shots.items())
    rows += tuple(
        row
        for field, k in components
        for row in component_rows(study.validation, study.checkpoints, field, k, minutes[1])
    )
    c = fit_shrinkage(
        tuple(o.predicted for o in study.training_outcomes),
        tuple(o.observed for o in study.training_outcomes),
    )
    predicted = tuple(
        evaluate("calibration", p=o.predicted, c=c).result for o in study.validation_outcomes
    )
    actual = tuple(o.observed for o in study.validation_outcomes)
    bins = calibration_bins(predicted, actual, params.calibration_bins.value)
    brier = evaluate("brier", predicted=predicted, observed=actual).result
    prior = evaluate(
        "brier",
        predicted=tuple(o.prior_predicted for o in study.validation_outcomes),
        observed=actual,
    ).result
    projection_passed = all(
        metric(r, "blend") <= min(metric(r, "prior"), metric(r, "current")) for r in rows
    )
    calibration_passed = (
        all(
            b.count > 0
            and b.difference is not None
            and b.difference <= params.calibration_alert.value
            for b in bins
        )
        and brier < prior
    )
    evidence = Evidence(
        kind="backtest",
        reference=input_sha256,
        as_of=study.training.players.as_of,
        reason=(
            f"Fitted on {study.training.league.season_id}; "
            f"independent holdout {study.validation.league.season_id}"
        ),
    )
    fitted_params = params.model_copy(
        update={
            "version": params.version + ":fit:" + input_sha256[:12],
            "rate_k": {s: NumberParameter(value=k, evidence=evidence) for s, k in fitted.items()},
            "shot_k": {s: NumberParameter(value=k, evidence=evidence) for s, k in shots.items()},
            "minute_k": NumberParameter(value=minutes[0], evidence=evidence),
            "minute_half_life": NumberParameter(value=minutes[1], evidence=evidence),
            "calibration": NumberParameter(value=c, evidence=evidence),
        }
    )
    availability, availability_rows = availability_fit(
        availability_samples(study.training_availability, study.training, set(params.availability)),
        availability_samples(
            study.validation_availability, study.validation, set(params.availability)
        ),
        params,
    )
    trials = tuple(
        production_trial(
            study.training,
            fitted_params.model_copy(
                update={"production_sigma": NumberParameter(value=sigma, evidence=evidence)}
            ),
            study.checkpoints,
        )
        for sigma in study.production_sigma_candidates
    )
    selected = min(trials, key=lambda row: (metric(row, "selected_mae"), -metric(row, "sigma")))
    fitted_params = fitted_params.model_copy(
        update={
            "availability": {
                status: NumberParameter(value=q, evidence=evidence)
                for status, q in availability.items()
            },
            "production_sigma": NumberParameter(value=metric(selected, "sigma"), evidence=evidence),
        }
    )
    production_validation = production_trial(study.validation, fitted_params, study.checkpoints)
    # The report names the parameters that were evaluated, not the initial guess.
    return BacktestReport(
        training_season=study.training.league.season_id,
        validation_season=study.validation.league.season_id,
        input_sha256=input_sha256,
        parameters_sha256=digest(canonical(fitted_params)),
        fitted_k=fitted,
        fitted_calibration=c,
        availability_rows=availability_rows,
        production_rows=tuple({**row, "partition": "training"} for row in trials)
        + ({**production_validation, "partition": "validation"},),
        availability_passed=all(
            metric(row, "brier") <= metric(row, "baseline_brier") for row in availability_rows
        ),
        production_passed=metric(production_validation, "selected_mae")
        <= metric(production_validation, "model_mae")
        and metric(production_validation, "flag_count") > 0,
        fitted_parameters=fitted_params,
        flag_rows=flag_report(study.validation, fitted_params, study.checkpoints),
        projection_rows=rows,
        calibration_rows=tuple(b.model_dump(mode="json") for b in bins),
        blended_brier=brier,
        prior_brier=prior,
        projection_passed=projection_passed,
        calibration_passed=calibration_passed,
        dataset_evidence=study.dataset_evidence,
        unverified=(
            "An offline study executes the supplied data; source authenticity "
            "and complete season coverage need separate evidence",
            "Recommendation recall and causal policy replay need dated league histories",
        ),
    )


def component_rows(
    season: ProjectionStudySeason,
    checkpoints: tuple[int, ...],
    field: str,
    k: float,
    half_life: float,
) -> tuple[dict[str, JsonValue], ...]:
    boxes = observed_boxes(season.players, season.players.as_of + timedelta(microseconds=1))
    priors = {p.player_id: p for p in season.priors.players}
    zone = ZoneInfo(season.league.timezone)
    shot = next((s for s in season.league.shots if s.id == field), None)
    rows: list[dict[str, JsonValue]] = []
    for checkpoint in checkpoints:
        errors: dict[str, list[float]] = {"blend": [], "prior": [], "current": []}
        for pid, history in boxes.items():
            current = tuple(
                b
                for b in history
                if season.league.starts_on
                <= b.played_at.astimezone(zone).date()
                <= season.league.ends_on
            )
            if len(current) <= checkpoint or pid not in priors:
                continue
            past, future = current[:checkpoint], current[checkpoint:]
            if shot is None:
                prior = priors[pid].minutes
                observed = evaluate("mean", values=tuple(b.minutes for b in future)).result
                raw = evaluate("mean", values=tuple(b.minutes for b in past)).result
                mixed = evaluate(
                    "minutes",
                    k=k,
                    prior=prior,
                    minutes=tuple(b.minutes for b in past),
                    half_life=half_life,
                ).result
            else:
                prior = priors[pid].probabilities[shot.id]
                attempts = fsum(b.stats[shot.attempted] for b in past)
                remaining = fsum(b.stats[shot.attempted] for b in future)
                if remaining == 0:
                    continue
                made = fsum(b.stats[shot.made] for b in past)
                observed = evaluate(
                    "ratio",
                    numerator=fsum(b.stats[shot.made] for b in future),
                    denominator=remaining,
                    zero_value=0.0,
                ).result
                raw = evaluate(
                    "ratio", numerator=made, denominator=attempts, zero_value=prior
                ).result
                mixed = evaluate("blend", k=k, prior=prior, total=made, sample=attempts).result
            for name, value in (("blend", mixed), ("prior", prior), ("current", raw)):
                errors[name].append(abs(value - observed))
        if not errors["blend"]:
            raise DataError(f"backtest.{checkpoint}.{field}: no holdout observations")
        rows.append(
            {
                "checkpoint": checkpoint,
                "stat": field,
                "players": len(errors["blend"]),
                **{
                    name: evaluate("mean", values=tuple(values)).result
                    for name, values in errors.items()
                },
            }
        )
    return tuple(rows)


def component_error(
    season: ProjectionStudySeason,
    checkpoints: tuple[int, ...],
    field: str,
    k: float,
    half_life: float,
) -> float:
    return fsum(
        metric(row, "blend") for row in component_rows(season, checkpoints, field, k, half_life)
    )


def flag_report(
    season: ProjectionStudySeason, params: InseasonParameters, checkpoints: tuple[int, ...]
) -> tuple[dict[str, JsonValue], ...]:
    from fba.inseason.projection import effective_projection

    boxes = observed_boxes(season.players, season.players.as_of + timedelta(microseconds=1))
    results: dict[str, list[bool]] = {kind: [] for kind in ("role", "production", "override")}
    zone = ZoneInfo(season.league.timezone)
    for checkpoint in checkpoints:
        for pid, history in boxes.items():
            current = tuple(
                b
                for b in history
                if season.league.starts_on
                <= b.played_at.astimezone(zone).date()
                <= season.league.ends_on
            )
            if len(current) <= checkpoint:
                continue
            at = max(b.known_at for b in current[:checkpoint]) + timedelta(microseconds=1)
            projection = effective_projection(
                season.players,
                season.priors,
                season.ledger,
                season.league,
                params,
                at,
                at.astimezone(zone).date(),
            )
            player = next((p for p in projection.players if p.player.id == pid), None)
            if player is None:
                continue
            future = tuple(b for b in current[checkpoint:] if b.played_at > at and b.known_at > at)
            if not future or not fsum(b.minutes for b in future):
                continue
            for flag in player.flags:
                if flag.kind not in results:
                    continue
                observed = (
                    evaluate("mean", values=tuple(b.minutes for b in future)).result
                    if flag.field == "minutes"
                    else evaluate(
                        "ratio",
                        numerator=fsum(b.stats[flag.field] for b in future),
                        denominator=fsum(b.minutes for b in future),
                        zero_value=0.0,
                    ).result
                )
                results.setdefault(flag.kind, []).append(
                    abs(flag.observed - observed) < abs(flag.model - observed)
                )
    return tuple(
        {
            "kind": kind,
            "count": len(values),
            "hit_rate": evaluate("mean", values=tuple(float(v) for v in values)).result
            if values
            else None,
        }
        for kind, values in sorted(results.items())
    )
