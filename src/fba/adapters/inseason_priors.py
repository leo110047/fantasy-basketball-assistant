from datetime import datetime
from math import isclose
from pathlib import Path

from fba.contracts.base import DataError
from fba.contracts.config import CalculationModel
from fba.contracts.data import Forecast
from fba.contracts.inseason import FrozenPriors, InseasonForecast, PlayerPrior, ProjectionRules
from fba.core.inseason import required_statistics
from fba.data.codec import digest, read_bytes
from fba.data.inseason_sources import forecast_document
from fba.formulas.registry import evaluate


def read_priors(
    path: Path, sha256: str, league: ProjectionRules, known_at: datetime
) -> FrozenPriors:
    data = read_bytes(path)
    if digest(data) != sha256:
        raise DataError(f"{path}: preseason forecast SHA-256 differs from the pinned version")
    archive = forecast_document(data, str(path))
    if archive.config.season.season_id != league.season_id:
        raise DataError(f"{path}: forecast season differs from league")
    if isinstance(archive, InseasonForecast):
        validate_direct_priors(archive.priors, league, known_at, str(path))
        return archive.priors.model_copy(update={"source_sha256": sha256})
    if not isinstance(archive.config.model, CalculationModel):
        raise DataError(f"{path}: projection model format does not contain per-game priors")
    parameters = archive.config.model.projection
    axes = (*parameters.stat_ids, parameters.threshold_stat)
    required = required_statistics(league)
    rows: list[PlayerPrior] = []
    for projected in archive.calculation.projections:
        if projected.expected_games > archive.season_games:
            raise DataError(f"{path}.{projected.id}: expected games exceed season length")
        stats = dict(zip(axes, projected.stats, strict=True))
        missing = set(required) - stats.keys()
        if missing:
            raise DataError(f"{path}.{projected.id}: missing prior statistics {sorted(missing)}")
        if projected.minutes == 0:
            if any(stats[s] > 0 for s in required):
                raise DataError(
                    f"{path}.{projected.id}: nonzero prior production with zero minutes"
                )
            rows.append(
                PlayerPrior(
                    player_id=projected.id,
                    appearance_probability=0.0,
                    minutes=0.0,
                    rates=dict.fromkeys(required, 0.0),
                    probabilities={},
                )
            )
            continue  # An explicit zero role must not acquire a peer's rotation minutes.
        probabilities: dict[str, float] = {}
        for shot in league.shots:
            if shot.made not in required or stats[shot.attempted] == 0:
                continue  # A missing probability is resolved from the configured peer prior.
            probabilities[shot.id] = evaluate(
                "ratio",
                numerator=stats[shot.made],
                denominator=stats[shot.attempted],
                zero_value=0.0,
            ).result
        rows.append(
            PlayerPrior(
                player_id=projected.id,
                appearance_probability=evaluate(
                    "ratio",
                    numerator=projected.expected_games,
                    denominator=float(archive.season_games),
                    zero_value=0.0,
                ).result,
                minutes=projected.minutes,
                rates={
                    s: evaluate(
                        "ratio", numerator=stats[s], denominator=projected.minutes, zero_value=0.0
                    ).result
                    for s in required
                },
                probabilities=probabilities,
            )
        )
    return FrozenPriors(
        format_version=1,
        season_id=league.season_id,
        version=str(archive.format_version),
        source_sha256=sha256,
        known_at=known_at,
        players=tuple(rows),
        distribution=parameters,
    )


def validate_direct_priors(
    priors: FrozenPriors, league: ProjectionRules, known_at: datetime, label: str
) -> None:
    if priors.season_id != league.season_id or priors.known_at != known_at:
        raise DataError(f"{label}: direct prior season or publication time differs from source")
    ids = tuple(p.player_id for p in priors.players)
    if not ids or len(set(ids)) != len(ids):
        raise DataError(f"{label}: empty or duplicate direct player priors")
    required = set(required_statistics(league))
    for player in priors.players:
        if player.appearance_probability is None or required - player.rates.keys():
            raise DataError(f"{label}.{player.player_id}: incomplete direct prior")
        if player.minutes == 0 and any(player.rates.values()):
            raise DataError(f"{label}.{player.player_id}: production with zero minutes")
        for shot in league.shots:
            if shot.made not in required:
                continue
            made, attempted = player.rates[shot.made], player.rates[shot.attempted]
            if made > attempted:
                raise DataError(
                    f"{label}.{player.player_id}.{shot.id}: made count exceeds attempts"
                )
            if attempted == 0:
                continue
            probability = player.probabilities.get(shot.id)
            expected = evaluate(
                "ratio", numerator=made, denominator=attempted, zero_value=0.0
            ).result
            if probability is None or not isclose(probability, expected):
                raise DataError(
                    f"{label}.{player.player_id}.{shot.id}: "
                    "missing or inconsistent prior probability"
                )


def direct_prior(
    forecast: Forecast,
    player_id: str,
    league: ProjectionRules,
    season_games: int,
    fallback_totals: dict[str, float] | None = None,
) -> PlayerPrior:
    """Convert provider totals to conditional rates without revising supplied forecasts.

    Missing statistics require an explicitly supplied fallback. Its use is recorded
    separately; zero is never substituted for an unavailable provider statistic.
    """
    if season_games <= 0 or forecast.expected_games > season_games:
        raise DataError(f"prior.{player_id}: expected games exceed season length")
    values = exact_forecast_totals(forecast, league)
    required = (*required_statistics(league), "MIN")
    supplied = {s: value for s in required if (value := values.get(s)) is not None}
    fallback = fallback_totals or {}
    missing = tuple(s for s in required if s not in supplied)
    absent = tuple(s for s in missing if s not in fallback)
    if absent:
        raise DataError(f"prior.{player_id}: unavailable forecast statistics {absent}")
    totals = {**{s: fallback[s] for s in missing}, **supplied}
    if any(value < 0 for value in totals.values()):
        raise DataError(f"prior.{player_id}: negative forecast or fallback statistic")
    minutes = totals["MIN"]
    if forecast.expected_games == 0 or minutes == 0:
        if any(totals.values()):
            raise DataError(f"prior.{player_id}: production with zero games or minutes")
        conditional_minutes = 0.0
    else:
        conditional_minutes = evaluate(
            "ratio", numerator=minutes, denominator=forecast.expected_games, zero_value=0.0
        ).result
    rates = {
        s: evaluate("ratio", numerator=totals[s], denominator=minutes, zero_value=0.0).result
        for s in required_statistics(league)
    }
    probabilities: dict[str, float] = {}
    for shot in league.shots:
        if shot.made not in rates:
            continue
        if totals[shot.made] > totals[shot.attempted]:
            raise DataError(f"prior.{player_id}.{shot.id}: made count exceeds attempts")
        if totals[shot.attempted]:
            probabilities[shot.id] = evaluate(
                "ratio",
                numerator=totals[shot.made],
                denominator=totals[shot.attempted],
                zero_value=0.0,
            ).result
    return PlayerPrior(
        player_id=player_id,
        appearance_probability=evaluate(
            "ratio",
            numerator=forecast.expected_games,
            denominator=float(season_games),
            zero_value=0.0,
        ).result,
        minutes=conditional_minutes,
        rates=rates,
        probabilities=probabilities,
        source=forecast.source_id,
        fallback_statistics=missing,
    )


def exact_forecast_totals(forecast: Forecast, league: ProjectionRules) -> dict[str, float | None]:
    """Recover a missing scoring component only when an exact identity determines it."""
    values = {s.id: s.value for s in forecast.totals}
    if len(values) != len(forecast.totals):
        raise DataError(f"prior.{forecast.player_id}: duplicate forecast statistics")
    for derived in league.derived:
        total = values.get(derived.id)
        if derived.kind != "linear" or total is None:
            continue
        missing = tuple(t for t in derived.terms if values.get(t.stat_id) is None)
        if len(missing) != 1 or missing[0].coefficient == 0:
            continue
        known = tuple(
            (value, term.coefficient)
            for term in derived.terms
            if (value := values.get(term.stat_id)) is not None
        )
        subtotal = evaluate(
            "linear", values=tuple(v for v, _ in known), weights=tuple(c for _, c in known)
        ).result
        inferred = evaluate(
            "ratio",
            numerator=evaluate("difference", before=subtotal, after=total).result,
            denominator=missing[0].coefficient,
            zero_value=0.0,
        ).result
        if inferred < 0:
            raise DataError(f"prior.{forecast.player_id}: negative derived scoring component")
        values[missing[0].stat_id] = inferred
    return values
