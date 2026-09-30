from math import fsum

from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import TeamConstraintModel, TeamOffenseModel, ThresholdCount
from fba.contracts.projection import (
    OffenseInput,
    PlayerMinuteAllocation,
    PlayerOffenseAllocation,
    PreparedPlayer,
    ProductionInput,
    Projected,
    ProjectionPlayer,
    TeamBoxPrior,
    TeamMinuteAllocation,
    TeamOffenseAllocation,
    TeamOffenseBaseline,
)
from fba.formulas.distribution import moments
from fba.formulas.projection import prior
from fba.formulas.registry import evaluate


def used(stats: tuple[float, ...], model: TeamOffenseModel) -> float:
    return fsum(
        t.coefficient * stats[model.projection.stat_ids.index(t.stat_id)]
        for t in model.team_offense.used_terms
    )


def supported_row(
    allocation: PlayerMinuteAllocation,
    projections: dict[str, Projected],
    outside: dict[str, TeamBoxPrior],
    model: TeamOffenseModel,
) -> PlayerOffenseAllocation | None:
    projected = projections.get(allocation.catalog_id or "")
    if projected is not None:
        stats = projected.stats[: len(model.projection.stat_ids)]
        games = projected.expected_games
        minutes = projected.minutes
    else:
        box = outside.get(allocation.member_id)
        if box is None or not box.priors:
            return None
        _, minutes, stats = prior(
            ProjectionPlayer(
                id=allocation.member_id,
                name=allocation.member_id,
                team_id=None,
                priors=box.priors,
                games_cap=None,
                return_on=None,
                history=(),
            ),
            model.projection,
        )
        games = allocation.expected_games_after
    return PlayerOffenseAllocation(
        member_id=allocation.member_id,
        catalog_id=allocation.catalog_id,
        expected_games=games,
        minutes=minutes,
        prior_coverage=allocation.prior_coverage,
        before=stats,
        after=stats,
        usage_factor=1,
        assist_factor=1,
    )


def validate_offense_input(inputs: OffenseInput, model: TeamOffenseModel) -> None:
    outside = {p.member_id: p for p in inputs.outside_priors}
    expected = {m.id: m for m in inputs.team_members if m.catalog_id is None}
    if len(outside) != len(inputs.outside_priors) or set(outside) != set(expected):
        raise DataError("team_offense.outside_priors: incomplete or duplicate roster coverage")
    for key, box in outside.items():
        estimates = {e.prior_id: e for e in expected[key].estimates}
        if not box.priors:
            continue
        if len({p.id for p in box.priors}) != len(box.priors) or {p.id for p in box.priors} != set(
            estimates
        ):
            raise DataError("team_offense.outside_priors: prior coverage differs from minutes")
        if not box.source_ids or any(
            p.expected_games != estimates[p.id].expected_games
            or p.minutes != estimates[p.id].minutes
            for p in box.priors
        ):
            raise DataError("team_offense.outside_priors: unsourced or inconsistent participation")
    teams = {b.team_id for b in inputs.offense_baselines}
    if len(teams) != len(inputs.offense_baselines) or teams != {t.id for t in inputs.teams}:
        raise DataError("team_offense.baselines: incomplete or duplicate team coverage")
    for baseline in inputs.offense_baselines:
        games = baseline.game_ids
        excluded = baseline.excluded_game_ids
        if (
            len(baseline.stats) != len(model.projection.stat_ids)
            or not baseline.source_ids
            or len(games) < model.team_offense.historical_minimum_games
            or len(set(games)) != len(games)
            or len(set(excluded)) != len(excluded)
            or set(games) & set(excluded)
        ):
            raise DataError("team_offense.baselines: invalid statistics, games, or provenance")


def allocate_usage(
    rows: tuple[PlayerOffenseAllocation, ...], budget: float, full: int, model: TeamOffenseModel
) -> tuple[PlayerOffenseAllocation, ...]:
    if isinstance(model, TeamConstraintModel) and model.team_constraints.offense == "audit":
        return tuple(sorted(rows, key=lambda r: r.member_id))
    result: list[PlayerOffenseAllocation] = []
    axes = model.projection.stat_ids
    demand = fsum(r.expected_games * used(r.before, model) / full for r in rows)
    factor = evaluate("budget_fraction", budget=budget, demand=demand).result
    # Source coverage measures evidence, not a player's right to the remaining possessions.
    for row in rows:
        stats = [
            v * factor if s in model.team_offense.scaled_stats else v
            for s, v in zip(axes, row.before, strict=True)
        ]
        stats[axes.index(model.projection.scoring_stat)] = fsum(
            t.coefficient * stats[axes.index(t.stat_id)] for t in model.projection.scoring_terms
        )
        result.append(row.model_copy(update={"after": tuple(stats), "usage_factor": factor}))
    assist, made = (
        axes.index(s) for s in (model.team_offense.assist_stat, model.team_offense.made_stat)
    )
    assists = fsum(r.expected_games * r.after[assist] for r in result)
    makes = fsum(r.expected_games * r.after[made] for r in result)
    factor = evaluate("budget_fraction", budget=makes, demand=assists).result
    return tuple(
        r.model_copy(
            update={
                "after": tuple(v * factor if i == assist else v for i, v in enumerate(r.after)),
                "assist_factor": factor,
            }
        )
        for r in sorted(result, key=lambda r: r.member_id)
    )


def allocate_team_offense(
    inputs: OffenseInput,
    minutes: TeamMinuteAllocation,
    baseline: TeamOffenseBaseline,
    projections: dict[str, Projected],
    outside: dict[str, TeamBoxPrior],
    model: TeamOffenseModel,
) -> TeamOffenseAllocation:
    rows = tuple(
        row
        for a in minutes.allocations
        if (row := supported_row(a, projections, outside, model)) is not None
    )
    full = next(t.full_season_games for t in inputs.teams if t.id == minutes.team_id)
    second = model.projection.stat_ids.index(model.team_offense.second_chance_stat)
    budget = used(baseline.stats, model) - baseline.stats[second]
    if budget <= 0:
        raise DataError(f"team_offense.{minutes.team_id}: nonpositive historical possession proxy")
    regulation = model.team_minutes.regulation_minutes * model.team_minutes.players_on_court
    # Assumption: carry forward regulation-normalized usage, including configured expected OT.
    capacity = minutes.budget
    budget *= capacity / regulation
    residual = max(0.0, capacity - fsum(r.expected_games * r.minutes / full for r in rows))
    reserve = budget * residual / capacity
    second_chances = fsum(r.expected_games * r.before[second] / full for r in rows)
    allocated = allocate_usage(rows, budget - reserve + second_chances, full, model)
    modeled = {r.member_id for r in rows}
    return TeamOffenseAllocation(
        team_id=minutes.team_id,
        baseline_games=len(baseline.game_ids),
        unmodeled_ids=tuple(
            sorted(
                m.id
                for m in inputs.team_members
                if m.team_id == minutes.team_id and m.id not in modeled
            )
        ),
        residual_minutes=residual,
        possession_budget=budget,
        reserved_possessions=reserve,
        before=fsum(r.expected_games * used(r.before, model) / full for r in rows) - second_chances,
        after=fsum(r.expected_games * used(r.after, model) / full for r in allocated)
        - second_chances,
        allocations=allocated,
    )


def offense_allocations(
    inputs: ProductionInput,
    projections: tuple[Projected, ...],
    minutes: tuple[TeamMinuteAllocation, ...],
) -> tuple[TeamOffenseAllocation, ...]:
    model = inputs.config.model
    if not isinstance(model, TeamOffenseModel):
        if isinstance(inputs, OffenseInput):
            raise ConfigError("team_offense: input requires matching offense model settings")
        return ()
    if not isinstance(inputs, OffenseInput):
        raise ConfigError("team_offense: rebuild an input with complete team offense sources")
    validate_offense_input(inputs, model)
    baselines = {b.team_id: b for b in inputs.offense_baselines}
    outside = {p.member_id: p for p in inputs.outside_priors}
    by_id = {p.id: p for p in projections}
    return tuple(
        allocate_team_offense(inputs, m, baselines[m.team_id], by_id, outside, model)
        for m in minutes
    )


def constrain_offense(
    projections: tuple[Projected, ...],
    inputs: OffenseInput,
    allocations: tuple[TeamOffenseAllocation, ...],
    threshold: ThresholdCount,
) -> tuple[Projected, ...]:
    model = inputs.config.model
    assert isinstance(model, TeamOffenseModel)
    changes = {
        p.catalog_id: p for a in allocations for p in a.allocations if p.catalog_id is not None
    }
    players = {p.id: p for p in inputs.players}
    pools = {p.id: p.history for p in inputs.history_pools}
    result: list[Projected] = []
    for projected in projections:
        change = changes[projected.id]
        if change.before == change.after:
            result.append(projected)
            continue
        player: PreparedPlayer = players[projected.id]
        if player.history_pool_id is not None:
            player = player.model_copy(update={"history": pools[player.history_pool_id]})
        adjusted = moments(
            player,
            projected.expected_games,
            projected.minutes,
            change.after,
            model.projection,
            threshold,
            model.valuation.result_decimals,
        )
        result.append(
            projected.model_copy(
                update={"stats": adjusted.stats, "covariance": adjusted.covariance}
            )
        )
    return tuple(result)


def validate_offense(
    projections: tuple[Projected, ...],
    inputs: OffenseInput,
    allocations: tuple[TeamOffenseAllocation, ...],
) -> None:
    model = inputs.config.model
    assert isinstance(model, TeamOffenseModel)
    if isinstance(model, TeamConstraintModel) and model.team_constraints.offense == "audit":
        return
    by_id = {p.id: p for p in projections}
    axes = model.projection.stat_ids
    second, assist, made = (
        axes.index(s)
        for s in (
            model.team_offense.second_chance_stat,
            model.team_offense.assist_stat,
            model.team_offense.made_stat,
        )
    )
    for allocation in allocations:
        full = next(t.full_season_games for t in inputs.teams if t.id == allocation.team_id)
        rows = [
            (
                r.expected_games / full,
                by_id[r.catalog_id].stats if r.catalog_id is not None else r.after,
            )
            for r in allocation.allocations
        ]
        precision = 10.0**-model.valuation.result_decimals
        tolerance = (
            precision
            * fsum(w for w, _ in rows)
            * (2 + fsum(t.coefficient for t in model.team_offense.used_terms))
        )
        total = fsum(w * (used(s, model) - s[second]) for w, s in rows)
        if total + allocation.reserved_possessions > allocation.possession_budget + tolerance:
            raise DataError(
                f"team_offense.{allocation.team_id}: projected usage exceeds team budget"
            )
        if fsum(w * (s[assist] - s[made]) for w, s in rows) > tolerance:
            raise DataError(f"team_offense.{allocation.team_id}: assists exceed made baskets")
