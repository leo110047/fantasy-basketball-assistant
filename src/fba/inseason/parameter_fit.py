"""Causal availability and flag fitting from explicit historical observations."""

from zoneinfo import ZoneInfo

from pydantic import JsonValue

from fba.contracts.base import DataError
from fba.contracts.inseason import AdjustmentLedger, EffectiveProjection, InseasonParameters
from fba.contracts.inseason_backtest import AvailabilityObservation, ProjectionStudySeason
from fba.formulas.registry import evaluate
from fba.inseason.checkpoint_history import checkpoint_history
from fba.inseason.projection import effective_projection


def availability_samples(
    observations: tuple[AvailabilityObservation, ...],
    season: ProjectionStudySeason,
    statuses: set[str],
) -> dict[str, tuple[float, ...]]:
    values: dict[str, list[float]] = {status: [] for status in sorted(statuses)}
    identities: set[tuple[str, str]] = set()
    player_ids = {p.id for p in season.players.players}
    zone = ZoneInfo(season.league.timezone)
    for row in observations:
        identity = row.player_id, row.game_id
        if identity in identities:
            raise DataError(f"backtest.availability: duplicate player/game {identity}")
        identities.add(identity)
        if row.player_id not in player_ids or row.status not in values:
            raise DataError("backtest.availability: unknown player or status")
        if not row.known_at < row.tipoff <= row.outcome_known_at <= season.players.as_of:
            raise DataError("backtest.availability: pregame status and published outcome required")
        if (
            not season.league.starts_on
            <= row.tipoff.astimezone(zone).date()
            <= season.league.ends_on
        ):
            raise DataError("backtest.availability: game outside the study season")
        values[row.status].append(float(row.played))
    missing = [status for status, samples in values.items() if not samples]
    if missing:
        raise DataError("backtest.availability: missing observations for " + ", ".join(missing))
    return {status: tuple(samples) for status, samples in values.items()}


def availability_fit(
    training: dict[str, tuple[float, ...]],
    validation: dict[str, tuple[float, ...]],
    params: InseasonParameters,
) -> tuple[dict[str, float], tuple[dict[str, JsonValue], ...]]:
    fitted = {
        status: evaluate("mean", values=samples).result for status, samples in training.items()
    }
    rows: list[dict[str, JsonValue]] = []
    for status, observed in validation.items():
        q, baseline = fitted[status], params.availability[status].value
        rows.append(
            {
                "status": status,
                "training_count": len(training[status]),
                "validation_count": len(observed),
                "fitted": q,
                "observed": evaluate("mean", values=observed).result,
                "baseline": baseline,
                "brier": evaluate(
                    "brier", predicted=(q,) * len(observed), observed=observed
                ).result,
                "baseline_brier": evaluate(
                    "brier", predicted=(baseline,) * len(observed), observed=observed
                ).result,
            }
        )
    return fitted, tuple(rows)


def production_trial(
    season: ProjectionStudySeason, params: InseasonParameters, checkpoints: tuple[int, ...]
) -> dict[str, JsonValue]:
    zone = ZoneInfo(season.league.timezone)
    errors: list[float] = []
    baseline: list[float] = []
    hits: list[float] = []
    projection: EffectiveProjection | None = None
    for checkpoint in checkpoints:
        for trial in checkpoint_history(season, checkpoint):
            pid, at, future = trial.player_id, trial.as_of, trial.future
            if projection is None or projection.as_of != at:
                projection = effective_projection(
                    season.players,
                    season.priors,
                    AdjustmentLedger(format_version=1, entries=()),
                    season.league,
                    params,
                    at,
                    at.astimezone(zone).date(),
                )
            player = next((p for p in projection.players if p.player.id == pid), None)
            if player is None or not future:
                continue
            minutes = sum(b.minutes for b in future)
            if minutes == 0:
                continue
            flags = {flag.field: flag for flag in player.flags if flag.kind == "production"}
            for stat, model in player.rates.items():
                actual = evaluate(
                    "ratio",
                    numerator=sum(b.stats[stat] for b in future),
                    denominator=minutes,
                    zero_value=0.0,
                ).result
                flag = flags.get(stat)
                suggested = flag.observed if flag else model
                baseline.append(evaluate("absolute_error", predicted=model, observed=actual).result)
                errors.append(
                    evaluate("absolute_error", predicted=suggested, observed=actual).result
                )
                if flag:
                    hits.append(float(errors[-1] < baseline[-1]))
    if not errors:
        raise DataError("backtest.production_sigma: no causal future observations")
    return {
        "sigma": params.production_sigma.value,
        "samples": len(errors),
        "flag_count": len(hits),
        "hit_rate": evaluate("mean", values=tuple(hits)).result if hits else None,
        "selected_mae": evaluate("mean", values=tuple(errors)).result,
        "model_mae": evaluate("mean", values=tuple(baseline)).result,
    }
