from collections import defaultdict
from math import fsum
from pathlib import Path

from fba.adapters.sources import SourceData
from fba.adapters.team_minutes import team_observations
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import TeamOffenseModel, ValidatedConfig
from fba.contracts.data import Forecast, Snapshot
from fba.contracts.projection import (
    PreparationNote,
    Prior,
    TeamBoxPrior,
    TeamMember,
    TeamOffenseBaseline,
)
from fba.formulas.registry import evaluate
from fba.projection.preparation import complete_forecast, game_samples


def outside_prior(
    member: TeamMember,
    forecast: Forecast | None,
    history: tuple[tuple[float, ...], ...],
    donors: tuple[tuple[float, ...], ...],
    model: TeamOffenseModel,
    historical_sources: tuple[str, ...],
) -> TeamBoxPrior:
    priors: list[Prior] = []
    notes: list[PreparationNote] = []
    sources = {s for e in member.estimates for s in e.source_ids}
    for estimate in member.estimates:
        if estimate.prior_id == model.preparation.historical_prior_id and history:
            priors.append(
                Prior(
                    id=estimate.prior_id,
                    expected_games=estimate.expected_games,
                    minutes=estimate.minutes,
                    stats=tuple(
                        evaluate("mean", values=tuple(r[i] for r in history)).result
                        for i in range(len(model.projection.stat_ids))
                    ),
                )
            )
        elif estimate.prior_id == model.preparation.forecast_prior_id and forecast is not None:
            use_individual = bool(history) and all(
                fsum(r[model.projection.stat_ids.index(s.parent_stat)] for r in history) > 0
                for s in model.preparation.history_shares
            )
            selected = history if use_individual else donors
            prior, inferred = complete_forecast(forecast, selected, model)
            notes.extend(n.model_copy(update={"player_id": member.id}) for n in inferred)
            if any(n.kind == "Assumption" for n in inferred):
                sources.update(historical_sources)
                notes.append(
                    PreparationNote(
                        player_id=member.id,
                        kind="Assumption",
                        detail="Missing count shares use "
                        + (
                            "individual history"
                            if use_individual
                            else "all-player historical totals"
                        ),
                    )
                )
            if prior is not None:
                priors.append(prior)
    if len(priors) != len(member.estimates):
        priors = []
    if not priors:
        notes.append(
            PreparationNote(
                player_id=member.id,
                kind="unavailable",
                detail=(
                    "No complete box prior; residual team minutes retain collective usage reserve"
                ),
            )
        )
    return TeamBoxPrior(
        member_id=member.id,
        priors=tuple(priors),
        source_ids=tuple(sorted(sources)),
        notes=tuple(notes),
    )


def offense_baselines(data: SourceData, model: TeamOffenseModel) -> tuple[TeamOffenseBaseline, ...]:
    axes = (*model.projection.stat_ids, model.preparation.minutes_stat)
    boxes: dict[tuple[str, str], list[tuple[float, ...]]] = defaultdict(list)
    sources: dict[str, set[str]] = defaultdict(set)
    for game in sorted(data.history, key=lambda g: (g.team_id, g.game_id, g.player_id)):
        stats = {s.id: s.value for s in game.stats}
        if len(stats) != len(game.stats) or any(stats.get(s) is None for s in axes):
            raise DataError("team_offense.history: duplicate or missing statistic")
        boxes[game.team_id, game.game_id].append(
            tuple(v for s in axes if (v := stats[s]) is not None)
        )
        sources[game.team_id].add(game.source_id)
    accepted: dict[str, list[tuple[str, tuple[float, ...]]]] = defaultdict(list)
    excluded: dict[str, list[str]] = defaultdict(list)
    regulation = model.team_minutes.regulation_minutes * model.team_minutes.players_on_court
    overtime = model.team_offense.historical_overtime_minutes * model.team_minutes.players_on_court
    tolerance = model.team_offense.historical_minute_tolerance
    for (team, game), rows in sorted(boxes.items()):
        totals = tuple(fsum(r[i] for r in rows) for i in range(len(axes)))
        minutes = totals[-1]
        periods = round((minutes - regulation) / overtime)
        if (
            minutes <= 0
            or periods < 0
            or abs(minutes - regulation - periods * overtime) > tolerance
        ):
            excluded[team].append(game)
            continue
        accepted[team].append(
            (
                game,
                tuple(
                    evaluate(
                        "ratio",
                        numerator=evaluate("product", gain=v, probability=regulation).result,
                        denominator=minutes,
                        zero_value=0.0,
                    ).result
                    for v in totals[:-1]
                ),
            )
        )
    result: list[TeamOffenseBaseline] = []
    for team in sorted({p.team_id for p in data.players if p.team_id is not None}):
        rows = accepted[team]
        if len(rows) < model.team_offense.historical_minimum_games:
            raise DataError(f"team_offense.{team}: insufficient minutes-screened historical games")
        result.append(
            TeamOffenseBaseline(
                team_id=team,
                stats=tuple(
                    evaluate("mean", values=tuple(r[i] for _, r in rows)).result
                    for i in range(len(model.projection.stat_ids))
                ),
                game_ids=tuple(g for g, _ in rows),
                excluded_game_ids=tuple(excluded[team]),
                source_ids=tuple(sorted(sources[team])),
            )
        )
    return tuple(result)


def offense_sources(
    root: Path, snapshot: Snapshot, config: ValidatedConfig, members: tuple[TeamMember, ...]
) -> tuple[tuple[TeamBoxPrior, ...], tuple[TeamOffenseBaseline, ...]]:
    model = config.model
    if not isinstance(model, TeamOffenseModel):
        raise ConfigError("team_offense: input requires matching offense model settings")
    data = team_observations(root, snapshot, config)
    samples, _ = game_samples(
        data.history, (*model.projection.stat_ids, model.preparation.minutes_stat)
    )
    history = {pid: tuple(r[:-1] for r in rows) for pid, rows in samples.items()}
    donors = tuple(r for pid in sorted(history) for r in history[pid])
    forecasts = {f.player_id: f for f in data.current}
    source_ids = tuple(sorted({g.source_id for g in data.history}))
    outside = tuple(
        outside_prior(
            m,
            forecasts.get(m.id.split(":", 1)[1]),
            history.get(m.id.split(":", 1)[1], ()),
            donors,
            model,
            source_ids,
        )
        for m in sorted(members, key=lambda m: m.id)
        if m.catalog_id is None
    )
    return outside, offense_baselines(data, model)
