from collections import Counter

from fba.adapters.acquisition import Acquired
from fba.adapters.codec import decode
from fba.adapters.roster import import_roster
from fba.adapters.sources import parse_sources
from fba.contracts.base import DataError
from fba.contracts.config import ValidatedConfig
from fba.contracts.data import (
    Artifact,
    CalibrationPair,
    Forecast,
    IdentityMap,
    ManualAdjustments,
    Multiply,
    PlayerGame,
    Snapshot,
)
from fba.core.data import fit_availability, resolve_players, validate_schedule


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
