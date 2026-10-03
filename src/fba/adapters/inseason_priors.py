from datetime import datetime
from pathlib import Path

from fba.contracts.archive import ForecastArchive
from fba.contracts.base import DataError
from fba.contracts.config import CalculationModel
from fba.contracts.inseason import FrozenPriors, PlayerPrior, ProjectionRules
from fba.core.inseason import required_statistics
from fba.data.codec import decode, digest, read_bytes
from fba.formulas.registry import evaluate


def read_priors(
    path: Path, sha256: str, league: ProjectionRules, known_at: datetime
) -> FrozenPriors:
    data = read_bytes(path)
    if digest(data) != sha256:
        raise DataError(f"{path}: preseason forecast SHA-256 differs from the pinned version")
    archive = decode(ForecastArchive, data, str(path))
    if archive.config.season.season_id != league.season_id:
        raise DataError(f"{path}: forecast season differs from league")
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
