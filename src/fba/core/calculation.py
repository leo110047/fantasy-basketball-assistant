from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import CalculationModel, ThresholdCount
from fba.contracts.projection import (
    CalculationResult,
    OffenseInput,
    PreparedInput,
    PreparedPlayer,
    ProductionInput,
    Projected,
    TeamOffenseAllocation,
)
from fba.core.distribution import moments
from fba.core.projection import calibrate_availability, prior, validate_availability
from fba.core.team_minutes import constrain_participation, minute_allocations, validate_minutes
from fba.core.team_offense import constrain_offense, offense_allocations, validate_offense
from fba.core.valuation import fit_ruler, value


def calculate(inputs: ProductionInput, input_sha256: str) -> CalculationResult:
    return calculate_with_offense(inputs, input_sha256)[0]


def calculate_with_offense(
    inputs: ProductionInput, input_sha256: str
) -> tuple[CalculationResult, tuple[TeamOffenseAllocation, ...]]:
    model = inputs.config.model
    if not isinstance(model, CalculationModel):
        raise ConfigError(
            "model: projection requires format_version 3; migrate the input explicitly"
        )
    if (
        inputs.calibration.training_season_id != inputs.config.season.previous_season_id
        or inputs.calibration.sample_size < 2
        or not inputs.calibration.inputs_sha256
    ):
        raise DataError("calibration: requires a sourced fit from the previous season")
    ids = tuple(p.id for p in inputs.players)
    teams = {t.id: t for t in inputs.teams}
    if len(set(ids)) != len(ids) or len(teams) != len(inputs.teams):
        raise DataError("projection input: duplicate player or team IDs")
    if any(p.team_id not in teams for p in inputs.players if p.team_id is not None):
        raise DataError("projection input: unknown team ID")
    season_games = {t.full_season_games for t in inputs.teams}
    if len(season_games) != 1:
        raise DataError("projection input: unequal or missing full-season game counts")
    threshold = next(
        (
            s.definition
            for s in inputs.config.season.stat_definitions
            if s.id == model.projection.threshold_stat
        ),
        None,
    )
    if not isinstance(threshold, ThresholdCount):
        raise ConfigError("model.projection.threshold_stat: requires a threshold_count statistic")
    allocations = minute_allocations(inputs)
    raw = project_population(inputs, model, threshold)
    axes = (*model.projection.stat_ids, model.projection.threshold_stat)
    # Replacement eligibility stays on the original GP scale, before availability calibration.
    ruler = fit_ruler(raw, axes, inputs.config.league, model.valuation)
    projections = calibrate_availability(
        raw, inputs.calibration, next(iter(season_games)), model.valuation.result_decimals
    )
    by_id = {p.id: p for p in inputs.players}
    adjusted: list[Projected] = []
    for projection in projections:
        player = by_id[projection.id]
        if isinstance(player, PreparedPlayer) and player.expected_games_override is not None:
            projection = projection.model_copy(
                update={"expected_games": player.expected_games_override}
            )
        adjusted.append(projection)
    projections = tuple(adjusted)
    for projection in projections:
        player = by_id[projection.id]
        if player.team_id is not None:
            validate_availability(player, projection.expected_games, teams[player.team_id])
        if player.games_cap is not None and projection.expected_games > player.games_cap:
            raise DataError(f"projection.{player.id}: calibrated games exceed explicit cap")
    projections = constrain_participation(projections, allocations, model.valuation.result_decimals)
    validate_minutes(projections, inputs, allocations)
    offense = offense_allocations(inputs, projections, allocations)
    if isinstance(inputs, OffenseInput):
        projections = constrain_offense(projections, inputs, offense, threshold)
        validate_offense(projections, inputs, offense)
        adjusted_stats = {p.id: p for p in projections}
        ruler = fit_ruler(
            tuple(
                p.model_copy(
                    update={
                        "stats": adjusted_stats[p.id].stats,
                        "covariance": adjusted_stats[p.id].covariance,
                    }
                )
                for p in raw
            ),
            axes,
            inputs.config.league,
            model.valuation,
        )
    result = CalculationResult(
        format_version=1,
        algorithm="team-offense-projection-v1"
        if offense
        else "role-constrained-projection-v1"
        if allocations
        else "calibrated-prior-projection-v3",
        input_sha256=input_sha256,
        config=inputs.config.refs,
        projections=projections,
        valuation=value(
            projections,
            ids,
            axes,
            inputs.config.league,
            model.valuation,
            next(iter(season_games)),
            ruler,
        ),
    )
    return result, offense


def project_population(
    inputs: ProductionInput,
    model: CalculationModel,
    threshold: ThresholdCount,
) -> tuple[Projected, ...]:
    teams = {t.id: t for t in inputs.teams}
    projected: list[Projected] = []
    pools = (
        {p.id: p.history for p in inputs.history_pools} if isinstance(inputs, PreparedInput) else {}
    )
    if isinstance(inputs, PreparedInput) and len(pools) != len(inputs.history_pools):
        raise DataError("projection.history_pools: duplicate IDs")
    for player in sorted(inputs.players, key=lambda p: p.id):
        if player.team_id is None or not player.priors:
            continue
        games, minutes, stats = prior(player, model.projection)
        validate_availability(player, games, teams[player.team_id])
        if isinstance(player, PreparedPlayer) and player.history_pool_id is not None:
            if player.history or player.history_pool_id not in pools:
                raise DataError(f"projection.{player.id}: missing or conflicting history pool")
            player = player.model_copy(update={"history": pools[player.history_pool_id]})
        projected.append(
            moments(
                player,
                games,
                minutes,
                stats,
                model.projection,
                threshold,
                model.valuation.result_decimals,
            )
        )
    return tuple(projected)
