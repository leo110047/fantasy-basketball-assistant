from collections import Counter, defaultdict
from math import fsum
from pathlib import Path

from fba.adapters.acquisition import Acquired
from fba.adapters.sources import SourceData, parse_sources
from fba.contracts.base import DataError
from fba.contracts.config import PreparationModel, ValidatedConfig
from fba.contracts.data import Forecast, PlayerGame, Snapshot
from fba.contracts.projection import MinuteEstimate, TeamMember
from fba.data.codec import read_bytes


def minute_estimates(
    forecast: Forecast | None,
    history: tuple[PlayerGame, ...],
    complete: bool,
    model: PreparationModel,
    season_games: int,
) -> tuple[MinuteEstimate, ...]:
    p = model.preparation
    estimates: list[MinuteEstimate] = []
    if history and complete:
        minutes = [
            next((s.value for s in g.stats if s.id == p.minutes_stat), None) for g in history
        ]
        if any(v is None for v in minutes):
            raise DataError("team_minutes.history: missing minutes")
        played = [v for v in minutes if v is not None and v > 0]
        if played:
            estimates.append(
                MinuteEstimate(
                    prior_id=p.historical_prior_id,
                    expected_games=min(
                        season_games,
                        p.historical_games_upper,
                        max(p.historical_games_lower, len(played)),
                    ),
                    minutes=fsum(played) / len(played),
                    source_ids=tuple(sorted({g.source_id for g in history})),
                )
            )
    if forecast is not None and forecast.expected_games > 0:
        total = next((s.value for s in forecast.totals if s.id == p.minutes_stat), None)
        if total is not None:
            estimates.append(
                MinuteEstimate(
                    prior_id=p.forecast_prior_id,
                    expected_games=forecast.expected_games,
                    minutes=total / forecast.expected_games,
                    source_ids=(forecast.source_id,),
                )
            )
    return tuple(estimates)


def team_observations(root: Path, snapshot: Snapshot, config: ValidatedConfig) -> SourceData:
    sources: list[Acquired] = []
    for source in config.season.sources:
        matches = [
            a for a in snapshot.artifacts if a.provenance and a.provenance.source_id == source.id
        ]
        if len(matches) != 1 or matches[0].provenance is None:
            raise DataError(f"team_minutes.{source.id}: missing frozen source")
        entry = matches[0]
        assert entry.provenance is not None
        sources.append(
            Acquired(source=source, provenance=entry.provenance, data=read_bytes(root / entry.path))
        )
    data = parse_sources(config, tuple(sources))
    if any(p.provider != "espn" for p in data.players if p.team_id is not None):
        raise DataError("team_minutes: provider requires a matching observation adapter")
    forecast = {f.player_id: f for f in data.current}
    if len(forecast) != len(data.current):
        raise DataError("team_minutes: duplicate forecasts")
    if len({(g.player_id, g.game_id) for g in data.history}) != len(data.history):
        raise DataError("team_minutes: duplicate historical game")
    return data


def team_members(root: Path, snapshot: Snapshot, config: ValidatedConfig) -> tuple[TeamMember, ...]:
    model = config.model
    assert isinstance(model, PreparationModel)
    data = team_observations(root, snapshot, config)
    forecast = {f.player_id: f for f in data.current}
    logs: dict[str, list[PlayerGame]] = defaultdict(list)
    for game in data.history:
        logs[game.player_id].append(game)
    observed = Counter(g.player_id for g in data.history)
    actual = {p.player_id: p.games for p in data.actual}
    mapped = {
        (i.provider, i.provider_player_id): p.roster.id
        for p in snapshot.players
        for i in p.identities
    }
    counts = {c.team_id: c.announced + c.pending for c in data.counts}
    return tuple(
        TeamMember(
            id=f"{p.provider}:{p.id}",
            team_id=p.team_id,
            catalog_id=mapped.get((p.provider, p.id)),
            estimates=minute_estimates(
                forecast.get(p.id),
                tuple(logs[p.id]),
                observed[p.id] == actual.get(p.id, 0),
                model,
                counts[p.team_id],
            ),
        )
        for p in sorted(data.players, key=lambda p: (p.provider, p.id))
        if p.team_id is not None
    )
