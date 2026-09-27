from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import CalculationModel, ThresholdCount
from fba.contracts.projection import CalculationResult, CalibratedInput, Projected
from fba.core.distribution import moments
from fba.core.projection import calibrate_availability, prior, validate_availability
from fba.core.valuation import fit_ruler, value


def calculate(inputs: CalibratedInput, input_sha256: str) -> CalculationResult:
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
    projected: list[Projected] = []
    for player in sorted(inputs.players, key=lambda p: p.id):
        if player.team_id is None or not player.priors:
            continue
        games, minutes, stats = prior(player, model.projection)
        validate_availability(player, games, teams[player.team_id])
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
    raw = tuple(projected)
    axes = (*model.projection.stat_ids, model.projection.threshold_stat)
    # Replacement eligibility stays on the original GP scale, before availability calibration.
    ruler = fit_ruler(raw, axes, inputs.config.league, model.valuation)
    projections = calibrate_availability(
        raw, inputs.calibration, next(iter(season_games)), model.valuation.result_decimals
    )
    by_id = {p.id: p for p in inputs.players}
    for projection in projections:
        player = by_id[projection.id]
        if player.team_id is not None:
            validate_availability(player, projection.expected_games, teams[player.team_id])
        if player.games_cap is not None and projection.expected_games > player.games_cap:
            raise DataError(f"projection.{player.id}: calibrated games exceed explicit cap")
    return CalculationResult(
        format_version=1,
        algorithm="calibrated-prior-projection-v3",
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
