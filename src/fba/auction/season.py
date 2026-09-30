from datetime import date
from math import fsum

import numpy as np
from numpy.typing import NDArray

from fba.auction.managed import ManagedSeason
from fba.contracts.auction import AuctionInput
from fba.contracts.backtest import (
    HealthObservation,
    ReplayInput,
    ReplayResult,
    ScheduledReplayInput,
)
from fba.contracts.base import DataError
from fba.contracts.config import SeasonModel
from fba.contracts.season import ManagementInput, ManagementPolicy, SeasonKernel
from fba.core.replay_schedule import schedule_tape
from fba.core.roster import capacity
from fba.formulas.scoring import score_season


def validate_replay(
    inputs: ReplayInput, auction: AuctionInput
) -> tuple[SeasonModel, ManagementInput]:
    league = inputs.config.league
    model, managed = inputs.config.model, auction.management
    if inputs.config != auction.config or managed is None:
        raise DataError("replay.config: must match the frozen managed auction")
    if not isinstance(model, SeasonModel):
        raise DataError("replay.model: requires format_version 6")
    teams = tuple(t.id for t in inputs.teams)
    if len(teams) != league.teams or len(set(teams)) != len(teams):
        raise DataError("replay.teams: must uniquely cover configured league")
    if len({t.seed for t in inputs.teams}) != len(teams):
        raise DataError("replay.teams.seed: duplicate seed")
    ids = set(managed.sampling_ids)
    rostered = tuple(p for t in inputs.teams for p in t.roster)
    available = (*rostered, *inputs.free_agents)
    if len(set(available)) != len(available) or not set(available) <= ids:
        raise DataError("replay.ownership: duplicate or unknown players")
    projected = {p.id for p in auction.players if p.utility is not None and p.active}
    if not set(available) <= projected:
        raise DataError("replay.ownership: held and free-agent players require usable projections")
    if any(
        len(t.roster) != capacity(league) or t.streaming_slots > len(t.roster) for t in inputs.teams
    ):
        raise DataError("replay.teams: invalid roster size or streaming slots")
    regular = tuple(w.id for w in league.matchups if w.phase != "playoff")
    if any(
        p.week_id not in regular
        or p.home not in teams
        or (p.away is not None and p.away not in teams)
        for p in inputs.pairings
    ):
        raise DataError("replay.pairings: unknown team or non-regular week")
    for w in regular:
        pairs = tuple(p for p in inputs.pairings if p.week_id == w)
        held = tuple(t for p in pairs for t in (p.home, p.away) if t is not None)
        if len(held) != len(set(held)) or set(held) != set(teams):
            raise DataError(
                f"replay.pairings.{w}: each team must appear exactly once, including explicit byes"
            )

    return model, managed


def health_tape(
    inputs: ReplayInput, ids: tuple[str, ...]
) -> tuple[NDArray[np.bool_], NDArray[np.uint8]]:
    groups = tuple(g for group in inputs.config.league.injury_slots for g in (group,) * group.count)
    events = sorted(inputs.health, key=lambda e: (e.published_at, e.player_id))
    keys = tuple((e.published_at, e.player_id) for e in events)
    if len(set(keys)) != len(keys) or any(e.player_id not in ids for e in events):
        raise DataError("replay.health: duplicate observation or unknown player")
    health = np.zeros((1, len(inputs.decision_times), len(ids)), dtype=bool)
    eligibility = np.zeros((len(inputs.decision_times), len(ids), len(groups)), dtype=np.uint8)
    known: dict[str, HealthObservation] = {}
    next_event = 0
    for d, when in enumerate(inputs.decision_times):
        while next_event < len(events) and events[next_event].published_at <= when:
            event = events[next_event]
            known[event.player_id] = event
            next_event += 1
        if set(known) != set(ids):
            raise DataError(
                f"replay.health: missing initial status for {sorted(set(ids) - set(known))}"
            )
        for p, player_id in enumerate(ids):
            event = known[player_id]
            health[0, d, p] = event.available
            eligibility[d, p] = [event.status in g.eligible_statuses for g in groups]
    return health, eligibility


def actual_boxes(
    inputs: ReplayInput,
    manager: ManagedSeason,
    lineups: tuple[tuple[int, int, tuple[int, ...]], ...],
) -> tuple[tuple[tuple[float, ...], ...], ...]:
    expected = (
        {
            (day, player)
            for d, day in enumerate(manager.days)
            for p, player in enumerate(manager.ids)
            if manager.schedule.actual[d, p]
        }
        if manager.schedule is not None
        else {(day, p.id) for p in manager.players for day in p.game_days}
    )
    actual: dict[tuple[date, str], tuple[float, ...]] = {}
    for box in inputs.actual:
        key = (box.day, box.player_id)
        if key in actual or box.player_id not in manager.index or len(box.stats) != manager.k:
            raise DataError("replay.actual: duplicate game, unknown player or wrong stat axes")
        if key not in expected:
            raise DataError("replay.actual: game outside player schedule")
        actual[key] = box.stats
    if set(actual) != expected:
        raise DataError(
            "replay.actual: requires every scheduled player game, including explicit zero DNP rows"
        )
    totals: list[list[list[list[float]]]] = [
        [[[] for _ in manager.stat_ids] for _ in manager.weeks] for _ in inputs.teams
    ]
    for day, team, started in lineups:
        for p in started:
            key = (manager.days[day], manager.ids[p])
            if key not in expected:
                # A game canceled after selection contributes nothing; it is not a DNP record.
                continue
            values = actual[key]
            for k, value in enumerate(values):
                totals[team][int(manager.week[day])][k].append(value)
    return tuple(tuple(tuple(fsum(v) for v in week) for week in team) for team in totals)


def replay(
    inputs: ReplayInput, auction: AuctionInput, input_sha256: str, kernel: SeasonKernel
) -> ReplayResult:
    model, managed = validate_replay(inputs, auction)
    # Canonical player order is part of the frozen projection's deterministic sampling contract.
    health, eligibility = health_tape(inputs, managed.sampling_ids)
    manager = ManagedSeason(
        inputs.config.league,
        model.fit.model_copy(update={"health_samples": 1}),
        managed,
        auction.players,
        kernel,
        health,
        schedule=schedule_tape(inputs, managed)
        if isinstance(inputs, ScheduledReplayInput)
        else None,
    )
    if {w.id for w in manager.weeks} != {w.id for w in inputs.config.league.matchups}:
        raise DataError("replay.schedule: must cover all configured matchup weeks")
    rosters = tuple(tuple(sorted(manager.index[p] for p in t.roster)) for t in inputs.teams)
    pool = tuple(sorted(manager.index[p] for p in inputs.free_agents))
    policy = ManagementPolicy(
        streaming_slots=tuple(t.streaming_slots for t in inputs.teams),
        reserve_adds=model.management.reserve_adds,
        upgrades=inputs.upgrades,
    )
    run = kernel.run(
        manager.arrays(eligibility), rosters, pool, manager.tactics(policy, model.management), True
    )
    boxes = actual_boxes(
        inputs, manager, tuple((e.day, e.team, e.started) for e in run.events if e.kind == "lineup")
    )
    outcomes, table, champion = score_season(
        inputs.config.league,
        managed.stat_ids,
        inputs.teams,
        inputs.pairings,
        boxes,
        tuple(w.id for w in manager.weeks),
    )
    return ReplayResult(
        format_version=1,
        algorithm="published-schedule-management-v1"
        if isinstance(inputs, ScheduledReplayInput)
        else "causal-management-v2",
        config=inputs.config.refs,
        input_sha256=input_sha256,
        auction_sha256=inputs.auction_sha256,
        information_mode=inputs.information_mode,
        player_ids=manager.ids,
        team_ids=tuple(t.id for t in inputs.teams),
        days=manager.days,
        events=run.events,
        weekly_boxes=boxes,
        adds=tuple(tuple(tuple(int(n) for n in w) for w in t) for t in run.adds[0]),
        outcomes=outcomes,
        standings=table,
        champion=champion,
    )
