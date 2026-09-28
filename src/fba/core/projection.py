from math import fsum

from fba.contracts.base import DataError
from fba.contracts.config import ProjectionParameters
from fba.contracts.data import Calibration
from fba.contracts.projection import Projected, ProjectionPlayer, ProjectionTeam


def prior(
    player: ProjectionPlayer, parameters: ProjectionParameters
) -> tuple[float, float, tuple[float, ...]]:
    if not player.priors or any(len(p.stats) != len(parameters.stat_ids) for p in player.priors):
        raise DataError(f"projection.{player.id}: missing or incompatible priors")
    for estimate in player.priors:
        if any(
            estimate.stats[parameters.stat_ids.index(pair.child)]
            > estimate.stats[parameters.stat_ids.index(pair.parent)]
            for pair in parameters.nested_counts
        ):
            raise DataError(f"projection.{player.id}.{estimate.id}: nested count exceeds parent")
        if estimate.minutes == 0 and any(estimate.stats):
            raise DataError(f"projection.{player.id}.{estimate.id}: production with zero minutes")
    weights = prior_weights(tuple(p.id for p in player.priors), parameters)
    total = 1.0
    games = fsum(p.expected_games * w for p, w in zip(player.priors, weights, strict=True)) / total
    if player.games_cap is not None:
        games = min(games, player.games_cap)
    minutes = fsum(p.minutes * w for p, w in zip(player.priors, weights, strict=True)) / total
    stats = [
        fsum(p.stats[i] * w for p, w in zip(player.priors, weights, strict=True)) / total
        for i in range(len(parameters.stat_ids))
    ]
    stats[parameters.stat_ids.index(parameters.scoring_stat)] = fsum(
        term.coefficient * stats[parameters.stat_ids.index(term.stat_id)]
        for term in parameters.scoring_terms
    )
    return games, minutes, tuple(stats)


def validate_availability(player: ProjectionPlayer, games: float, team: ProjectionTeam) -> None:
    if not team.dates or team.dates != tuple(sorted(set(team.dates))):
        raise DataError(f"projection.{team.id}: missing or unordered schedule")
    start = player.return_on or team.dates[0]
    capacity = (
        team.full_season_games if start <= team.dates[0] else sum(d >= start for d in team.dates)
    )
    if games > capacity:
        raise DataError(f"projection.{player.id}: expected games exceed eligible schedule")


def calibrate_availability(
    players: tuple[Projected, ...], calibration: Calibration, season_games: int, decimals: int
) -> tuple[Projected, ...]:
    return tuple(
        p.model_copy(
            update={
                "expected_games": calibrated_games(
                    p.expected_games, calibration, season_games, decimals
                )
            }
        )
        for p in players
    )


def prior_weights(ids: tuple[str, ...], parameters: ProjectionParameters) -> tuple[float, ...]:
    configured = {p.id: p.weight for p in parameters.prior_weights}
    if len(set(ids)) != len(ids) or any(i not in configured for i in ids):
        raise DataError("projection: duplicate or unconfigured prior ID")
    weights = (1.0,) if len(ids) == 1 else tuple(configured[i] for i in ids)
    total = fsum(weights)
    if total <= 0:
        raise DataError("projection: available priors have zero total weight")
    return tuple(w / total for w in weights)


def calibrated_games(
    games: float, calibration: Calibration, season_games: int, decimals: int
) -> float:
    return round(
        min(
            season_games,
            max(0, calibration.intercept + calibration.slope * round(games, decimals)),
        ),
        decimals,
    )
