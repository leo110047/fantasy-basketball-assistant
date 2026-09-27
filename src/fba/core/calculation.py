from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import CalculationModel, ThresholdCount
from fba.contracts.projection import CalculationResult, Projected, ProjectionInput
from fba.core.distribution import moments
from fba.core.projection import reconcile_team
from fba.core.valuation import value


def calculate(inputs: ProjectionInput, input_sha256: str) -> CalculationResult:
    model = inputs.config.model
    if not isinstance(model, CalculationModel):
        raise ConfigError("model: projection calculation requires format_version 2")
    ids = [p.id for p in inputs.players]
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
    for team in sorted(inputs.teams, key=lambda t: t.id):
        players = tuple(
            sorted(
                (p for p in inputs.players if p.team_id == team.id and p.priors), key=lambda p: p.id
            )
        )
        if not players:
            continue
        reconciled = reconcile_team(players, team, model.projection)
        for player, (games, minutes, stats) in zip(players, reconciled, strict=True):
            if player.catalog:
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
    projections = tuple(sorted(projected, key=lambda p: p.id))
    return CalculationResult(
        format_version=1,
        algorithm="configured-resource-projection-v1",
        input_sha256=input_sha256,
        config=inputs.config.refs,
        projections=projections,
        valuation=value(
            projections,
            tuple(p.id for p in inputs.players if p.catalog),
            (*model.projection.stat_ids, model.projection.threshold_stat),
            inputs.config.league,
            model.valuation,
            next(iter(season_games)),
        ),
    )
