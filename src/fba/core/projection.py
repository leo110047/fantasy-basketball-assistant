from math import fsum, isfinite

from fba.contracts.base import DataError
from fba.contracts.config import ProjectionParameters
from fba.contracts.projection import ProjectionPlayer, ProjectionTeam


def weighted_cap(
    target: tuple[float, ...],
    scales: tuple[float, ...],
    weights: tuple[float, ...],
    limit: float,
    parameters: ProjectionParameters,
) -> tuple[float, ...]:
    """Unique minimum of weighted squared adjustment under one resource limit."""
    if not target or len(target) != len(scales) or len(target) != len(weights):
        raise DataError("projection.allocation: incompatible axes")
    if not all(isfinite(x) and x >= 0 for x in (*target, *weights, limit)) or not all(
        isfinite(x) and x > 0 for x in scales
    ):
        raise DataError("projection.allocation: invalid target, scale, weight, or limit")
    if fsum(w * t for w, t in zip(weights, target, strict=True)) <= limit:
        return target
    cost = tuple(s * s * w for s, w in zip(scales, weights, strict=True))
    low = 0.0
    high = max(t / c for t, c in zip(target, cost, strict=True) if c > 0)
    for _ in range(parameters.search_iterations):
        middle = (low + high) / 2
        used = fsum(
            w * max(t - middle * c, 0) for w, t, c in zip(weights, target, cost, strict=True)
        )
        if used > limit:
            low = middle
        else:
            high = middle
    result = tuple(max(t - high * c, 0) for t, c in zip(target, cost, strict=True))
    if (
        fsum(w * x for w, x in zip(weights, result, strict=True))
        > limit + parameters.feasibility_tolerance
    ):
        raise DataError("projection.allocation: resource postcondition failed")
    return result


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
    games = fsum(p.expected_games for p in player.priors) / len(player.priors)
    if player.games_cap is not None:
        games = min(games, player.games_cap)
    minutes = fsum(p.minutes for p in player.priors) / len(player.priors)
    stats = tuple(
        fsum(p.stats[i] for p in player.priors) / len(player.priors)
        for i in range(len(parameters.stat_ids))
    )
    return games, minutes, stats


def exposure(player: ProjectionPlayer, games: float, team: ProjectionTeam) -> float:
    if not team.dates or team.dates != tuple(sorted(set(team.dates))):
        raise DataError(f"projection.{team.id}: missing or unordered schedule")
    start = player.return_on or team.dates[0]
    capacity = (
        team.full_season_games if start <= team.dates[0] else sum(d >= start for d in team.dates)
    )
    if games > capacity:
        raise DataError(f"projection.{player.id}: expected games exceed eligible schedule")
    # Return-only availability is monotone: the final window dominates every earlier window.
    return games / max(1, capacity) if start <= team.dates[-1] else 0


def reconcile_team(
    players: tuple[ProjectionPlayer, ...],
    team: ProjectionTeam,
    parameters: ProjectionParameters,
) -> tuple[tuple[float, float, tuple[float, ...]], ...]:
    originals = tuple(prior(p, parameters) for p in players)
    games = tuple(p[0] for p in originals)
    minutes = tuple(p[1] for p in originals)
    weights = tuple(exposure(p, gp, team) for p, gp in zip(players, games, strict=True))
    scales = tuple(
        max(
            parameters.minimum_cost_scale,
            p.minutes_sd,
            (max(v.minutes for v in p.priors) - min(v.minutes for v in p.priors)) / 2,
        )
        for p in players
    )
    allocated = weighted_cap(
        minutes,
        scales,
        weights,
        parameters.regulation_minutes * parameters.players_on_court,
        parameters,
    )
    boxes = [
        list(v * a / m if m > 0 else 0 for v in row[2])
        for row, a, m in zip(originals, allocated, minutes, strict=True)
    ]
    axes = parameters.stat_ids
    used = tuple(
        fsum(t.coefficient * row[axes.index(t.stat_id)] for t in parameters.possession_terms)
        for row in boxes
    )
    limit = team.possession_budget + fsum(
        w * row[axes.index(parameters.second_chance_stat)]
        for w, row in zip(weights, boxes, strict=True)
    )
    offense = weighted_cap(
        used,
        tuple(
            max(u * s / max(m, 1), parameters.minimum_usage_scale)
            for u, s, m in zip(used, scales, minutes, strict=True)
        ),
        weights,
        limit,
        parameters,
    )
    for row, before, after in zip(boxes, used, offense, strict=True):
        for field in parameters.offense_stats:
            row[axes.index(field)] *= after / before if before > 0 else 1
    assists = tuple(row[axes.index(parameters.assist_stat)] for row in boxes)
    assists = weighted_cap(
        assists,
        tuple(max(v, parameters.minimum_usage_scale) for v in assists),
        weights,
        fsum(
            w * row[axes.index(parameters.made_stat)] for w, row in zip(weights, boxes, strict=True)
        ),
        parameters,
    )
    for row, assist in zip(boxes, assists, strict=True):
        row[axes.index(parameters.assist_stat)] = assist
        row[axes.index(parameters.scoring_stat)] = fsum(
            t.coefficient * row[axes.index(t.stat_id)] for t in parameters.scoring_terms
        )
    result = tuple(
        (gp if mpg > 0 else 0, mpg, tuple(row))
        for gp, mpg, row in zip(games, allocated, boxes, strict=True)
    )
    validate_resources(result, weights, team, parameters)
    return result


def validate_resources(
    rows: tuple[tuple[float, float, tuple[float, ...]], ...],
    weights: tuple[float, ...],
    team: ProjectionTeam,
    parameters: ProjectionParameters,
) -> None:
    axes = parameters.stat_ids

    def total(field: str) -> float:
        return fsum(w * row[2][axes.index(field)] for w, row in zip(weights, rows, strict=True))

    bounds = (
        (
            fsum(w * row[1] for w, row in zip(weights, rows, strict=True)),
            parameters.regulation_minutes * parameters.players_on_court,
        ),
        (
            fsum(t.coefficient * total(t.stat_id) for t in parameters.possession_terms),
            team.possession_budget + total(parameters.second_chance_stat),
        ),
        (total(parameters.assist_stat), total(parameters.made_stat)),
    )
    if any(used > limit + parameters.feasibility_tolerance for used, limit in bounds):
        raise DataError(f"projection.{team.id}: final resource postcondition failed")
