from datetime import date, timedelta

import numpy as np
from numpy.typing import NDArray

from fba.contracts.backtest import ScheduledReplayInput, ScheduleObservation
from fba.contracts.base import DataError
from fba.contracts.config import SeasonConfig
from fba.contracts.season import ManagementInput, ReplaySchedule


def season_days(season: SeasonConfig) -> tuple[date, ...]:
    return tuple(
        season.starts_on + timedelta(days=i)
        for i in range((season.ends_on - season.starts_on).days + 1)
    )


def apply_schedule(
    current: NDArray[np.bool_],
    event: ScheduleObservation,
    days: tuple[date, ...],
    index: dict[str, int],
) -> None:
    dates = tuple(g.day for g in event.games)
    if event.player_id not in index or len(set(dates)) != len(dates) or not set(dates) <= set(days):
        raise DataError("replay.schedules: unknown player, duplicate date or game outside season")
    p = index[event.player_id]
    revised = np.array([d in dates for d in days], dtype=bool)
    # The adapter validates and localizes publication times to the league timezone.
    past = np.array([d < event.published_at.date() for d in days], dtype=bool)
    if np.any(current[past, p] != revised[past]):
        raise DataError("replay.schedules: an announcement cannot rewrite earlier local dates")
    current[:, p] = revised


def schedule_tape(inputs: ScheduledReplayInput, managed: ManagementInput) -> ReplaySchedule:
    """Build knowledge prefixes from validated, league-local observation timestamps."""
    days = season_days(inputs.config.season)
    if tuple(t.date() for t in inputs.decision_times) != days:
        raise DataError("replay.decision_times: version 2 must cover the configured season")
    players = {p.id: p for p in managed.players}
    ids = managed.sampling_ids
    index = {p: i for i, p in enumerate(ids)}
    if len(index) != len(ids) or len(players) != len(managed.players) or set(index) != set(players):
        raise DataError("replay.schedules: player axes must uniquely match managed projections")
    if any(not set(p.game_days) <= set(days) for p in players.values()):
        raise DataError("replay.schedules: initial calendar outside configured season")
    current = np.array([[d in players[p].game_days for p in ids] for d in days], dtype=bool)
    events = sorted(inputs.schedules, key=lambda e: (e.published_at, e.player_id))
    keys = tuple((e.published_at, e.player_id) for e in events)
    if len(set(keys)) != len(keys) or any(e.published_at > inputs.evaluated_at for e in events):
        raise DataError("replay.schedules: duplicate observation or publication after evaluation")
    if inputs.information_mode == "published" and any(
        e.published_at <= inputs.config.season.snapshot_as_of for e in events
    ):
        raise DataError("replay.schedules: changes must follow the initial published snapshot")
    known: list[NDArray[np.bool_]] = []
    cursor = 0
    for when in inputs.decision_times:
        if cursor < len(events) and events[cursor].published_at <= when:
            current = current.copy()
        while cursor < len(events) and events[cursor].published_at <= when:
            apply_schedule(current, events[cursor], days, index)
            cursor += 1
        known.append(current)
    actual = current.copy()
    for event in events[cursor:]:
        apply_schedule(actual, event, days, index)
    return ReplaySchedule(days=days, known=tuple(known), actual=actual)
