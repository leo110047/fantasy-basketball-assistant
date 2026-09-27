from math import fsum

from fba.contracts.base import DataError
from fba.contracts.config import ProjectionParameters
from fba.contracts.projection import ProjectionPlayer, ProjectionTeam


def prior(
    player: ProjectionPlayer, parameters: ProjectionParameters
) -> tuple[float, float, tuple[float, ...]]:
    if not player.priors or any(len(p.stats) != len(parameters.stat_ids) for p in player.priors):
        raise DataError(f"projection.{player.id}: missing or incompatible priors")
    configured = {p.id: p.weight for p in parameters.prior_weights}
    if len({p.id for p in player.priors}) != len(player.priors) or any(
        p.id not in configured for p in player.priors
    ):
        raise DataError(f"projection.{player.id}: duplicate or unconfigured prior ID")
    for estimate in player.priors:
        if any(
            estimate.stats[parameters.stat_ids.index(pair.child)]
            > estimate.stats[parameters.stat_ids.index(pair.parent)]
            for pair in parameters.nested_counts
        ):
            raise DataError(f"projection.{player.id}.{estimate.id}: nested count exceeds parent")
        if estimate.minutes == 0 and any(estimate.stats):
            raise DataError(f"projection.{player.id}.{estimate.id}: production with zero minutes")
    # A sole source is retained even when its configured blend weight is zero.
    weights = (1.0,) if len(player.priors) == 1 else tuple(configured[p.id] for p in player.priors)
    total = fsum(weights)
    if total <= 0:
        raise DataError(f"projection.{player.id}: available priors have zero total weight")
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
