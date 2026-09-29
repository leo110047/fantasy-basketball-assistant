from collections import defaultdict
from datetime import date
from math import fsum

from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import PreparationModel, PreparationParameters, ValidatedConfig
from fba.contracts.data import (
    Adjustment,
    ExpectedGames,
    Forecast,
    Multiply,
    Player,
    PlayerGame,
    Snapshot,
)
from fba.contracts.projection import (
    HistoryPool,
    PreparationNote,
    PreparedPlayer,
    PreparedPopulation,
    Prior,
    ProjectionTeam,
)


def position_pool(player: Player, parameters: PreparationParameters) -> str:
    positions = set(player.roster.positions)
    for group in parameters.position_pools:
        if positions & set(group.any_positions) or positions == set(group.exact_positions):
            return group.id
    raise DataError(f"preparation.{player.roster.id}: positions have no configured history pool")


def history_samples(
    snapshot: Snapshot, axes: tuple[str, ...]
) -> tuple[dict[str, tuple[tuple[float, ...], ...]], tuple[PreparationNote, ...]]:
    return game_samples(snapshot.history, axes)


def game_samples(
    games: tuple[PlayerGame, ...], axes: tuple[str, ...]
) -> tuple[dict[str, tuple[tuple[float, ...], ...]], tuple[PreparationNote, ...]]:
    grouped: dict[str, list[tuple[float, ...]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    notes: list[PreparationNote] = []
    for game in sorted(games, key=lambda g: (g.player_id, g.game_id)):
        key = (game.player_id, game.game_id)
        values = {s.id: s.value for s in game.stats}
        if key in seen or len(values) != len(game.stats):
            raise DataError(f"preparation.history.{key}: duplicate observation")
        seen.add(key)
        row: list[float] = []
        for stat in axes:
            value = values.get(stat)
            if value is None:
                raise DataError(f"preparation.history.{key}.{stat}: missing statistic")
            row.append(value)
        if row[-1] == 0:
            notes.append(
                PreparationNote(
                    player_id=game.player_id,
                    kind="source",
                    detail=f"Excluded reported zero-minute record {game.game_id}",
                )
            )
            continue
        grouped[game.player_id].append(tuple(row))
    return {pid: tuple(rows) for pid, rows in grouped.items()}, tuple(notes)


def history_pools(
    snapshot: Snapshot,
    samples: dict[str, tuple[tuple[float, ...], ...]],
    forecasts: dict[str, Forecast],
    parameters: PreparationParameters,
) -> tuple[HistoryPool, ...]:
    pools: dict[str, list[tuple[float, ...]]] = defaultdict(list)
    for player in sorted(snapshot.players, key=lambda p: p.roster.id):
        rows = samples.get(player.roster.id, ())
        if player.history_status != "available" or len(rows) < parameters.donor_minimum_history:
            continue
        minutes = fsum(r[-1] for r in rows) / len(rows)
        forecast = forecasts.get(player.roster.id)
        if forecast is not None and forecast.expected_games > 0:
            projected_minutes = next(
                (s.value for s in forecast.totals if s.id == parameters.minutes_stat), None
            )
            if projected_minutes is not None:
                minutes = projected_minutes / forecast.expected_games
        if parameters.donor_minutes_lower <= minutes <= parameters.donor_minutes_upper:
            pools[position_pool(player, parameters)].extend(r[:-1] for r in rows)
    return tuple(HistoryPool(id=pid, history=tuple(rows)) for pid, rows in sorted(pools.items()))


def complete_forecast(
    forecast: Forecast, history: tuple[tuple[float, ...], ...], model: PreparationModel
) -> tuple[Prior | None, tuple[PreparationNote, ...]]:
    if forecast.expected_games <= 0:
        return None, (
            PreparationNote(
                player_id=forecast.player_id,
                kind="unavailable",
                detail="Forecast prior unavailable: nonpositive GP",
            ),
        )
    p, settings = model.projection, model.preparation
    values = {s.id: s.value for s in forecast.totals}
    known_values = {key: value for key, value in values.items() if value is not None}
    if len(values) != len(forecast.totals):
        raise DataError(f"preparation.{forecast.player_id}: duplicate forecast statistics")
    notes: list[PreparationNote] = []
    scoring = values.get(p.scoring_stat)
    missing = [t for t in p.scoring_terms if values.get(t.stat_id) is None]
    if scoring is not None and len(missing) == 1 and missing[0].coefficient != 0:
        term = missing[0]
        known = fsum(
            t.coefficient * known_values[t.stat_id]
            for t in p.scoring_terms
            if t != term and values[t.stat_id] is not None
        )
        inferred = (scoring - known) / term.coefficient
        if inferred < -p.feasibility_tolerance:
            raise DataError(
                f"preparation.{forecast.player_id}.{term.stat_id}: negative derived count"
            )
        values[term.stat_id] = max(0, inferred)
        notes.append(
            PreparationNote(
                player_id=forecast.player_id,
                kind="derived",
                detail=f"{term.stat_id}: solved from configured scoring identity",
            )
        )
    for share in settings.history_shares:
        parent = values.get(share.parent_stat)
        if values.get(share.stat_id) is not None or parent is None:
            continue
        numerator = fsum(r[p.stat_ids.index(share.stat_id)] for r in history)
        denominator = fsum(r[p.stat_ids.index(share.parent_stat)] for r in history)
        if denominator <= 0:
            raise DataError(
                f"preparation.{forecast.player_id}.{share.stat_id}: "
                "historical share denominator is zero"
            )
        values[share.stat_id] = parent * numerator / denominator
        notes.append(
            PreparationNote(
                player_id=forecast.player_id,
                kind="Assumption",
                detail=(
                    f"{share.stat_id}: projected {share.parent_stat} "
                    "times selected historical share"
                ),
            )
        )
    required = (*p.stat_ids, settings.minutes_stat)
    absent = [s for s in required if values.get(s) is None]
    if absent:
        note = PreparationNote(
            player_id=forecast.player_id,
            kind="unavailable",
            detail=f"Forecast prior unavailable: missing {absent}",
        )
        return None, (*notes, note)
    totals = tuple(value for s in required if (value := values[s]) is not None)
    gp = forecast.expected_games
    return Prior(
        id=settings.forecast_prior_id,
        expected_games=gp,
        minutes=totals[-1] / gp,
        stats=tuple(v / gp for v in totals[:-1]),
    ), tuple(notes)


def adjust_player(
    player: PreparedPlayer,
    adjustments: tuple[Adjustment, ...],
    model: PreparationModel,
    team: ProjectionTeam,
    season_start: date,
) -> PreparedPlayer:
    priors = player.priors
    expected: float | None = None
    return_on: date | None = None
    for adjustment in sorted(adjustments, key=lambda a: (a.published_at, a.id)):
        operation = adjustment.operation
        if adjustment.effective_from.date() > season_start:
            raise DataError(
                f"manual_adjustments.{adjustment.id}: "
                "midseason effective changes require season simulation"
            )
        if isinstance(operation, Multiply):
            if operation.stat_id == model.preparation.minutes_stat:
                priors = tuple(
                    p.model_copy(update={"minutes": p.minutes * operation.factor}) for p in priors
                )
            elif (
                operation.stat_id in model.projection.stat_ids
                and operation.stat_id != model.projection.scoring_stat
            ):
                index = model.projection.stat_ids.index(operation.stat_id)
                priors = tuple(
                    p.model_copy(
                        update={
                            "stats": tuple(
                                v * operation.factor if i == index else v
                                for i, v in enumerate(p.stats)
                            )
                        }
                    )
                    for p in priors
                )
            else:
                raise DataError(
                    f"manual_adjustments.{adjustment.id}: "
                    "adjust primitive statistics, not derived totals"
                )
        elif isinstance(operation, ExpectedGames):
            expected = operation.games
        else:
            return_on = operation.return_at.date()
    capacity = (
        team.full_season_games
        if return_on is None or return_on <= team.dates[0]
        else sum(d >= return_on for d in team.dates)
    )
    if expected is not None and expected > capacity:
        raise DataError(f"manual_adjustments.{player.id}: expected games exceed eligible schedule")
    if expected is not None:
        priors = tuple(p.model_copy(update={"expected_games": expected}) for p in priors)
    return player.model_copy(
        update={
            "priors": priors,
            "expected_games_override": expected,
            "return_on": return_on,
            "games_cap": float(capacity) if return_on is not None else None,
        }
    )


def prepare_player(
    player: Player,
    forecast: Forecast | None,
    samples: tuple[tuple[float, ...], ...],
    pools: dict[str, HistoryPool],
    model: PreparationModel,
    team: ProjectionTeam,
) -> tuple[PreparedPlayer, tuple[PreparationNote, ...]]:
    pid, parameters = player.roster.id, model.preparation
    notes: list[PreparationNote] = []
    priors: list[Prior] = []
    if samples and player.history_status == "available":
        gp = min(
            team.full_season_games,
            parameters.historical_games_upper,
            max(parameters.historical_games_lower, len(samples)),
        )
        means = tuple(fsum(r[i] for r in samples) / len(samples) for i in range(len(samples[0])))
        priors.append(
            Prior(
                id=parameters.historical_prior_id,
                expected_games=gp,
                minutes=means[-1],
                stats=means[:-1],
            )
        )
        notes.append(
            PreparationNote(
                player_id=pid,
                kind="Assumption",
                detail=(
                    "Historical prior: previous-season means; GP clipped to configured "
                    f"[{parameters.historical_games_lower}, {parameters.historical_games_upper}]"
                ),
            )
        )
    pool_id: str | None = None
    history = tuple(r[:-1] for r in samples)
    if player.history_status != "available" or len(samples) < parameters.minimum_player_history:
        pool_id = position_pool(player, parameters)
        selected = pools.get(pool_id)
        history = () if selected is None else selected.history
        notes.append(
            PreparationNote(
                player_id=pid,
                kind="Assumption",
                detail=(
                    f"History status={player.history_status}; {len(samples)} individual games; "
                    f"use position pool {pool_id}. This does not imply rookie status."
                ),
            )
        )
    if forecast is not None:
        estimate, estimates = complete_forecast(forecast, history, model)
        notes.extend(estimates)
        if estimate is not None:
            priors.append(estimate)
    if priors and not history:
        raise DataError(f"preparation.{pid}: required individual or position history unavailable")
    if not priors:
        pool_id = None
        notes.append(
            PreparationNote(
                player_id=pid,
                kind="unavailable",
                detail=(
                    "No usable forecast or complete individual history; fair value remains missing"
                ),
            )
        )
    return PreparedPlayer(
        id=pid,
        name=player.roster.name,
        team_id=player.team_id,
        priors=tuple(priors),
        games_cap=None,
        return_on=None,
        history=history if pool_id is None else (),
        expected_games_override=None,
        history_pool_id=pool_id,
    ), tuple(notes)


def prepare(snapshot: Snapshot, config: ValidatedConfig) -> PreparedPopulation:
    model = config.model
    if not isinstance(model, PreparationModel):
        raise ConfigError("model: annual projection requires preparation format_version 4")
    if (
        snapshot.season_id != config.season.season_id
        or snapshot.as_of > config.season.snapshot_as_of
    ):
        raise DataError("preparation.snapshot: wrong season or future snapshot")
    player_ids = {p.roster.id for p in snapshot.players}
    if len(player_ids) != len(snapshot.players):
        raise DataError("preparation.players: duplicate IDs")
    if any(f.player_id not in player_ids for f in snapshot.forecasts) or any(
        g.player_id not in player_ids for g in snapshot.history
    ):
        raise DataError("preparation.observations: unknown player ID")
    teams = tuple(
        ProjectionTeam(
            id=c.team_id,
            dates=tuple(
                sorted(
                    g.local_date
                    for g in snapshot.schedule
                    if c.team_id in (g.home_team_id, g.away_team_id)
                    and g.status in ("scheduled", "completed")
                )
            ),
            full_season_games=c.announced + c.pending,
        )
        for c in sorted(snapshot.schedule_counts, key=lambda c: c.team_id)
    )
    by_team = {t.id: t for t in teams}
    samples, history_notes = history_samples(
        snapshot, (*model.projection.stat_ids, model.preparation.minutes_stat)
    )
    forecasts = {f.player_id: f for f in snapshot.forecasts}
    if len(forecasts) != len(snapshot.forecasts):
        raise DataError("preparation.forecasts: duplicate player forecasts")
    pools = history_pools(snapshot, samples, forecasts, model.preparation)
    by_pool = {p.id: p for p in pools}
    changes: dict[str, list[Adjustment]] = defaultdict(list)
    if len({a.id for a in snapshot.adjustments.adjustments}) != len(
        snapshot.adjustments.adjustments
    ):
        raise DataError("manual_adjustments: duplicate IDs")
    for adjustment in snapshot.adjustments.adjustments:
        if adjustment.player_id not in player_ids:
            raise DataError(f"manual_adjustments.{adjustment.id}: unknown player")
        if adjustment.published_at > config.season.snapshot_as_of:
            raise DataError(f"manual_adjustments.{adjustment.id}: future publication")
        changes[adjustment.player_id].append(adjustment)
    players: list[PreparedPlayer] = []
    notes = list(history_notes)
    for player in sorted(snapshot.players, key=lambda p: p.roster.id):
        if player.team_id is None:
            if changes.get(player.roster.id):
                raise DataError(
                    f"manual_adjustments.{player.roster.id}: no active team for adjustment"
                )
            players.append(
                PreparedPlayer(
                    id=player.roster.id,
                    name=player.roster.name,
                    team_id=None,
                    priors=(),
                    games_cap=None,
                    return_on=None,
                    history=(),
                    expected_games_override=None,
                    history_pool_id=None,
                )
            )
            notes.append(
                PreparationNote(
                    player_id=player.roster.id,
                    kind="unavailable",
                    detail="Not on an active team in the frozen roster",
                )
            )
            continue
        if player.team_id not in by_team:
            raise DataError(f"preparation.{player.roster.id}: unknown team")
        projected, player_notes = prepare_player(
            player,
            forecasts.get(player.roster.id),
            samples.get(player.roster.id, ()),
            by_pool,
            model,
            by_team[player.team_id],
        )
        adjustments = tuple(changes.get(player.roster.id, ()))
        players.append(
            adjust_player(
                projected, adjustments, model, by_team[player.team_id], config.season.starts_on
            )
        )
        notes.extend(player_notes)
        notes.extend(
            PreparationNote(
                player_id=player.roster.id,
                kind="manual",
                detail=(
                    f"{a.id}: {a.reason}; source={a.source}; "
                    f"published={a.published_at.isoformat()}; Assumption={a.assumption}"
                ),
            )
            for a in adjustments
        )
    used = {p.history_pool_id for p in players}
    return PreparedPopulation(
        players=tuple(players),
        teams=teams,
        notes=tuple(notes),
        history_pools=tuple(p for p in pools if p.id in used),
    )
