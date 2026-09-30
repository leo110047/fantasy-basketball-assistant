from fba.adapters import espn, nba, normalized
from fba.adapters.acquisition import Acquired
from fba.adapters.annual import validate_archive
from fba.contracts.archive import ForecastArchive
from fba.contracts.base import DataError, Record
from fba.contracts.config import ValidatedConfig
from fba.contracts.data import (
    ActualGames,
    Forecast,
    Game,
    PlayerGame,
    ProviderPlayer,
    ScheduleCount,
)
from fba.data.codec import decode


class SourceData(Record):
    current: tuple[Forecast, ...]
    previous: tuple[Forecast, ...]
    history: tuple[PlayerGame, ...]
    games: tuple[Game, ...]
    players: tuple[ProviderPlayer, ...]
    counts: tuple[ScheduleCount, ...]
    actual: tuple[ActualGames, ...]


def data_sources(config: ValidatedConfig, sources: tuple[Acquired, ...]) -> tuple[Acquired, ...]:
    result: list[Acquired] = []
    for acquired in sources:
        if acquired.source.role != "forecast_archive":
            result.append(acquired)
        else:
            if acquired.source.adapter != "fba_forecast":
                raise DataError(f"{acquired.source.id}: unsupported forecast archive adapter")
            validate_archive(decode(ForecastArchive, acquired.data, acquired.source.id), config)
    return tuple(result)


def parse_sources(
    config: ValidatedConfig,
    sources: tuple[Acquired, ...],
) -> SourceData:
    current: list[Forecast] = []
    previous: list[Forecast] = []
    history: list[PlayerGame] = []
    actual: list[ActualGames] = []
    games: list[Game] = []
    players: list[ProviderPlayer] = []
    catalog: dict[str, ProviderPlayer] = {}
    counts: list[ScheduleCount] = []
    releases: list[Acquired] = []
    for acquired in data_sources(config, sources):
        source, data = acquired.source, acquired.data
        if source.adapter.startswith("espn_"):
            espn.validate_source_season(source)
        if source.adapter == "espn_players" and source.role in (
            "projections",
            "historical_projections",
            "game_logs",
        ):
            wire = espn.decode_players(data, source.id)
            if source.role == "projections":
                for player in wire:
                    catalog[str(player.id)] = ProviderPlayer(
                        provider="espn",
                        id=str(player.id),
                        name=player.fullName,
                        team_id=None,
                    )
            if source.role == "game_logs":
                history.extend(espn.game_logs(wire, int(source.season_code), source.id))
                actual.extend(espn.actual_games(wire, int(source.season_code), source.id))
            else:
                target = current if source.role == "projections" else previous
                target.extend(espn.forecasts(wire, int(source.season_code), source.id))
        elif source.adapter == "espn_schedule" and source.role == "schedule":
            games.extend(
                espn.schedule(
                    data,
                    int(source.season_code),
                    source.id,
                    config.league.timezone,
                )
            )
        elif source.adapter in ("espn_roster", "espn_roster_page") and source.role == "rosters":
            reader = {"espn_roster": espn.roster, "espn_roster_page": espn.roster_page}[
                source.adapter
            ]
            players.extend(reader(data, int(source.season_code), source.id))
        elif source.adapter == "normalized_rosters" and source.role == "rosters":
            players.extend(normalized.rosters(data, config.season.season_id, source.id))
        elif source.adapter == "official_counts" and source.role == "official_schedule_counts":
            counts.extend(normalized.counts(data, config.season.season_id, source.id))
        elif source.adapter == "nba_release" and source.role == "official_schedule_counts":
            releases.append(acquired)
        else:
            raise DataError(f"{source.id}: unsupported adapter/role {source.adapter}/{source.role}")
    team_ids = tuple(sorted({team for g in games for team in (g.home_team_id, g.away_team_id)}))
    roster_teams = {p.team_id for p in players if p.team_id is not None}
    if roster_teams != set(team_ids):
        raise DataError("rosters: team coverage differs from schedule; cannot infer free agents")
    member_ids = {(p.provider, p.id) for p in players}
    if len(member_ids) != len(players):
        raise DataError("rosters: player appears more than once across team rosters")
    players.extend(p for p in catalog.values() if (p.provider, p.id) not in member_ids)
    for release in releases:
        counts.extend(
            nba.release_counts(
                release.data,
                config.season.season_id,
                release.source.id,
                team_ids,
            )
        )
    return SourceData(
        current=tuple(current),
        previous=tuple(previous),
        history=tuple(history),
        games=tuple(games),
        players=tuple(players),
        counts=tuple(counts),
        actual=tuple(actual),
    )
