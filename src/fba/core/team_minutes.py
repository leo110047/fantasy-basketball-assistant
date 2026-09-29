from math import fsum

from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import AvailabilityModel, TeamBudgetModel, TeamConstraintModel
from fba.contracts.projection import (
    BudgetedInput,
    PlayerMinuteAllocation,
    PreparedPlayer,
    ProductionInput,
    Projected,
    RoleProjected,
    TeamMember,
    TeamMinuteAllocation,
)
from fba.core.projection import calibrated_games, prior, prior_weights


def member_minutes(
    member: TeamMember,
    players: dict[str, PreparedPlayer],
    inputs: BudgetedInput,
    full: int,
) -> tuple[float, float] | None:
    model = inputs.config.model
    assert isinstance(model, TeamBudgetModel)
    if member.catalog_id is not None:
        player = players[member.catalog_id]
        if not player.priors:
            return None
        gp, minutes, _ = prior(player, model.projection)
        expected = player.expected_games_override
    else:
        if not member.estimates:
            return None
        weights = prior_weights(tuple(p.prior_id for p in member.estimates), model.projection)
        gp = fsum(p.expected_games * w for p, w in zip(member.estimates, weights, strict=True))
        minutes = fsum(p.minutes * w for p, w in zip(member.estimates, weights, strict=True))
        expected = None
    if (
        minutes
        > model.team_minutes.regulation_minutes + model.team_minutes.overtime_minutes_per_game
    ):
        raise DataError(f"team_minutes.{member.id}: player minutes exceed game budget")
    if gp > full:
        raise DataError(f"team_minutes.{member.id}: expected games exceed full season")
    if expected is None:
        expected = calibrated_games(
            gp,
            inputs.calibration,
            full,
            model.valuation.result_decimals,
            model.availability_tail if isinstance(model, AvailabilityModel) else None,
        )
    return expected, minutes


def minute_allocations(inputs: ProductionInput) -> tuple[TeamMinuteAllocation, ...]:
    model = inputs.config.model
    if not isinstance(model, TeamBudgetModel):
        return ()
    if not isinstance(inputs, BudgetedInput):
        raise ConfigError("team_minutes: rebuild a complete-population projection input")
    players = {p.id: p for p in inputs.players}
    members = inputs.team_members
    teams = {t.id: t for t in inputs.teams}
    mapped = tuple(m.catalog_id for m in members if m.catalog_id is not None)
    if len({m.id for m in members}) != len(members) or len(set(mapped)) != len(mapped):
        raise DataError("team_minutes: duplicate member or catalogue mapping")
    if set(mapped) != {p.id for p in players.values() if p.team_id is not None}:
        raise DataError("team_minutes: active catalogue coverage mismatch")
    if {m.team_id for m in members} != set(teams) or any(
        m.catalog_id is not None and players[m.catalog_id].team_id != m.team_id for m in members
    ):
        raise DataError("team_minutes: team coverage or mapping mismatch")
    return tuple(
        allocate_team(inputs, players, team.id)
        for team in sorted(teams.values(), key=lambda t: t.id)
    )


def allocate_team(
    inputs: BudgetedInput, players: dict[str, PreparedPlayer], team_id: str
) -> TeamMinuteAllocation:
    model = inputs.config.model
    assert isinstance(model, TeamBudgetModel)
    parameters = model.team_minutes
    enforce = (
        not isinstance(model, TeamConstraintModel) or model.team_constraints.minutes == "enforce"
    )
    full = next(t.full_season_games for t in inputs.teams if t.id == team_id)
    budget = parameters.players_on_court * (
        parameters.regulation_minutes + parameters.overtime_minutes_per_game
    )
    rows = tuple(
        sorted((m for m in inputs.team_members if m.team_id == team_id), key=lambda m: m.id)
    )
    values = tuple(member_minutes(m, players, inputs, full) for m in rows)
    demands = tuple(v for v in values if v is not None)
    weights = {p.id: p.weight for p in model.projection.prior_weights}
    groups: dict[tuple[bool, float], list[tuple[TeamMember, float, float]]] = {}
    for member, value in zip(rows, values, strict=True):
        if value is None:
            continue
        ids = (
            tuple(p.id for p in players[member.catalog_id].priors)
            if member.catalog_id is not None
            else tuple(p.prior_id for p in member.estimates)
        )
        coverage = fsum(weights[i] for i in ids)
        pinned = (
            member.catalog_id is not None
            and players[member.catalog_id].expected_games_override is not None
        )
        groups.setdefault((pinned, coverage), []).append((member, *value))
    remaining = budget - parameters.unmodeled_reserve_minutes
    allocated: list[PlayerMinuteAllocation] = []
    for pinned, coverage in sorted(groups, reverse=True):
        group = groups[pinned, coverage]
        usage = fsum(gp * minutes / full for _, gp, minutes in group)
        if enforce and pinned and usage > remaining:
            raise DataError(f"team_minutes.{team_id}: manual expected games exceed team budget")
        factor = min(1.0, remaining / usage) if enforce and usage and not pinned else 1.0
        for member, gp, minutes in group:
            allocated.append(
                PlayerMinuteAllocation(
                    member_id=member.id,
                    catalog_id=member.catalog_id,
                    expected_games_before=gp,
                    expected_games_after=gp * factor,
                    minutes=minutes,
                    prior_coverage=coverage,
                )
            )
        remaining = max(0.0, remaining - usage)
    allocations = tuple(sorted(allocated, key=lambda a: a.member_id))
    before = fsum(gp * minutes / full for gp, minutes in demands)
    after = fsum(a.expected_games_after * a.minutes / full for a in allocations)
    return TeamMinuteAllocation(
        team_id=team_id,
        members=len(rows),
        modeled_members=len(demands),
        unmodeled_ids=tuple(m.id for m, v in zip(rows, values, strict=True) if v is None),
        before=before,
        budget=budget,
        allocations=allocations,
        after=after,
        reserve=max(0.0, budget - after),
    )


def validate_minutes(
    projections: tuple[Projected, ...],
    inputs: ProductionInput,
    allocations: tuple[TeamMinuteAllocation, ...],
) -> None:
    if not allocations:
        return
    model = inputs.config.model
    assert isinstance(model, TeamBudgetModel)
    teams = {t.id: t for t in inputs.teams}
    memberships = {p.id: p.team_id for p in inputs.players}
    precision = 10.0**-model.valuation.result_decimals
    for allocation in allocations:
        rows = tuple(p for p in projections if memberships[p.id] == allocation.team_id)
        total = fsum(
            p.expected_games * p.minutes / teams[allocation.team_id].full_season_games for p in rows
        )
        rounding_bound = precision * fsum(
            (p.minutes + p.expected_games + precision) / teams[allocation.team_id].full_season_games
            for p in rows
        )
        if total > allocation.after + rounding_bound:
            raise DataError(
                f"team_minutes.{allocation.team_id}: catalogue exceeds allocated minutes"
            )


def constrain_participation(
    projections: tuple[Projected, ...],
    allocations: tuple[TeamMinuteAllocation, ...],
    decimals: int,
) -> tuple[Projected, ...]:
    by_id = {
        p.catalog_id: p for a in allocations for p in a.allocations if p.catalog_id is not None
    }
    result: list[Projected] = []
    for p in projections:
        allocation = by_id.get(p.id)
        if allocation is None:
            result.append(p)
            continue
        if p.expected_games != allocation.expected_games_before or p.minutes != round(
            allocation.minutes, decimals
        ):
            raise DataError(f"team_minutes.{p.id}: allocation differs from projected participation")
        result.append(
            RoleProjected(
                id=p.id,
                expected_games=round(allocation.expected_games_after, decimals),
                unconstrained_games=p.expected_games,
                minutes=p.minutes,
                stats=p.stats,
                covariance=p.covariance,
            )
        )
    return tuple(result)
