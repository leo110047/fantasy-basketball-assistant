from collections import Counter

from fba.adapters import espn, nba, normalized
from fba.adapters.acquisition import Acquired
from fba.adapters.annual import validate_archive
from fba.adapters.codec import decode
from fba.adapters.roster import import_roster
from fba.contracts.archive import ForecastArchive
from fba.contracts.base import DataError, Record
from fba.contracts.config import ValidatedConfig
from fba.contracts.data import (
    ActualGames,
    Artifact,
    CalibrationPair,
    Forecast,
    Game,
    IdentityMap,
    ManualAdjustments,
    Multiply,
    PlayerGame,
    ProviderPlayer,
    ScheduleCount,
    Snapshot,
)
from fba.core.data import fit_availability, resolve_players, validate_schedule


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


def map_observations(
    identities: IdentityMap,
    forecasts: tuple[Forecast, ...],
    history: tuple[PlayerGame, ...],
) -> tuple[tuple[Forecast, ...], tuple[PlayerGame, ...]]:
    by_provider = {
        i.provider_player_id: i.player_id for i in identities.entries if i.provider == "espn"
    }
    # Each observation remains traceable to its provider ID through the explicit identity map.
    current = tuple(
        Forecast(
            player_id=by_provider[f.player_id],
            expected_games=f.expected_games,
            totals=f.totals,
            source_id=f.source_id,
        )
        for f in forecasts
        if f.player_id in by_provider
    )
    logs = tuple(
        PlayerGame(
            player_id=by_provider[g.player_id],
            game_id=g.game_id,
            team_id=g.team_id,
            stats=g.stats,
            source_id=g.source_id,
        )
        for g in history
        if g.player_id in by_provider
    )
    return (
        tuple(sorted(current, key=lambda f: (f.player_id, f.source_id))),
        tuple(sorted(logs, key=lambda g: (g.player_id, g.game_id, g.source_id))),
    )


def assemble(
    config: ValidatedConfig,
    sources: tuple[Acquired, ...],
    roster_data: bytes,
    identities_data: bytes,
    adjustments_data: bytes,
    artifacts: tuple[Artifact, ...],
    version: int,
) -> Snapshot:
    identities = decode(IdentityMap, identities_data, "identity_map")
    adjustments = decode(ManualAdjustments, adjustments_data, "manual_adjustments")
    rows = import_roster(roster_data, config.season.roster_import, config.league.positions)
    validate_input_dates(config, identities, adjustments, frozenset(r.id for r in rows))
    data = parse_sources(config, sources)
    games = tuple(
        g for g in data.games if config.season.starts_on <= g.local_date <= config.season.ends_on
    )
    validate_schedule(games, data.counts)
    forecasts, logs = map_observations(identities, data.current, data.history)
    actual = {p.player_id: p.games for p in data.actual}
    observed = Counter(g.player_id for g in data.history)
    incomplete = frozenset(
        i.player_id
        for i in identities.entries
        if i.provider == "espn"
        and observed[i.provider_player_id] != actual.get(i.provider_player_id, 0)
    )
    players = resolve_players(
        rows,
        identities,
        data.players,
        frozenset(g.player_id for g in logs),
        incomplete,
    )
    pairs = tuple(
        CalibrationPair(
            player_id=f.player_id,
            projected_games=f.expected_games,
            actual_games=actual.get(f.player_id, 0),
        )
        for f in data.previous
    )
    training = tuple(
        a.provenance.raw_sha256
        for a in sources
        if a.source.role
        in (
            "game_logs",
            "historical_projections",
        )
    )
    calibration = fit_availability(
        pairs,
        config.season.previous_season_id,
        training,
        config.model.calibration.value,
    )
    return Snapshot(
        format_version=1,
        season_id=config.season.season_id,
        version=version,
        as_of=config.season.snapshot_as_of,
        config=config.refs,
        artifacts=artifacts,
        players=players,
        schedule=tuple(sorted(games, key=lambda g: (g.tipoff, g.id))),
        schedule_counts=tuple(sorted(data.counts, key=lambda c: c.team_id)),
        forecasts=forecasts,
        history=logs,
        calibration=calibration,
        adjustments=adjustments,
    )


def validate_input_dates(
    config: ValidatedConfig,
    identities: IdentityMap,
    adjustments: ManualAdjustments,
    player_ids: frozenset[str],
) -> None:
    cutoff = config.season.snapshot_as_of
    for entry in identities.entries:
        if entry.confirmed_at > cutoff:
            raise DataError(f"identity_map.{entry.player_id}: confirmed after snapshot cutoff")
        if entry.player_id not in player_ids:
            raise DataError(f"identity_map.{entry.player_id}: not in imported roster")
    if len({a.id for a in adjustments.adjustments}) != len(adjustments.adjustments):
        raise DataError("manual_adjustments: duplicate IDs")
    for adjustment in adjustments.adjustments:
        if adjustment.published_at > cutoff:
            raise DataError(f"manual_adjustments.{adjustment.id}: published after snapshot cutoff")
        if adjustment.player_id not in player_ids:
            raise DataError(f"manual_adjustments.{adjustment.id}: unknown player")
        if isinstance(adjustment.operation, Multiply) and adjustment.operation.stat_id not in {
            s.id for s in config.season.stat_definitions
        }:
            raise DataError(f"manual_adjustments.{adjustment.id}: unknown statistic")
