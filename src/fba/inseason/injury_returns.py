"""Conditional IL activation scenarios; no real Yahoo roster is changed."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from fba.contracts.base import DataError
from fba.contracts.inseason import InjuryReturn
from fba.inseason.matchup import Simulation

if TYPE_CHECKING:
    from fba.contracts.inseason import FantasyTeam


def returning_players(sim: Simulation, team: FantasyTeam) -> tuple[tuple[date, str, bool], ...]:
    today = sim.as_of.astimezone(sim.zone).date()
    players = {p.player.id: p for p in sim.projection(today).players}
    result: list[tuple[date, str, bool]] = []
    for pid, slot_id in team.injury_players.items():
        slot = next(s for s in sim.league.injury_slots if s.id == slot_id)
        p = players[pid].player
        if p.status not in slot.eligible_statuses:
            result.append((today, pid, False))
        elif p.return_on is not None and p.return_on > today:
            result.append((p.return_on, pid, True))
    return tuple(sorted(result))


def unconfirmed_returns(sim: Simulation, team_ids: tuple[str, ...]) -> tuple[str, ...]:
    today = sim.as_of.astimezone(sim.zone).date()
    players = {p.player.id: p.player for p in sim.projection(today).players}
    return tuple(
        sorted(
            pid
            for team in sim.snapshot.teams
            if team.id in team_ids
            for pid, slot_id in team.injury_players.items()
            if (player := players[pid]).return_on is not None
            and player.known_at.astimezone(sim.zone).date() < player.return_on <= today
            and player.status
            in next(s.eligible_statuses for s in sim.league.injury_slots if s.id == slot_id)
        )
    )


def reuse_return_plans(
    sim: Simulation, team_ids: tuple[str, ...]
) -> tuple[Simulation, tuple[str, ...]]:
    """Today reuses known IL decisions; a full roster needs a saved drop plan."""
    if not sim.project_injury_returns:
        return sim, ()
    pending = list(unconfirmed_returns(sim, team_ids))
    missing = tuple(
        t
        for t in sim.snapshot.teams
        if t.id in team_ids
        and t.injury_players
        and (
            (t.id, tuple(sorted(t.players))) not in sim.injury_plan_cache
            or (
                t.id == sim.snapshot.mine
                and any(
                    move.drop in sim.untouchable
                    for move in sim.injury_plan_cache[t.id, tuple(sorted(t.players))]
                )
            )
        )
    )
    if not missing:
        return sim, tuple(pending)
    child = Simulation(
        sim.league,
        sim.params,
        sim.players,
        sim.priors,
        sim.ledger,
        sim.snapshot,
        sim.as_of,
        sim.samples,
        untouchable=tuple(sim.untouchable),
    )
    child.projections, child.projection_profiles, child.draws = (
        sim.projections,
        sim.projection_profiles,
        sim.draws,
    )
    child.priority_cache = sim.priority_cache
    child.transitions = sim.transitions.copy()
    child.cancelled, child.deadline = sim.cancelled, sim.deadline
    child.injury_plan_cache = sim.injury_plan_cache.copy()
    capacity = len(sim.league.starter_slots) + sim.league.bench_slots
    for team in missing:
        active = team.players
        planned: list[InjuryReturn] = []
        for on, pid, estimated in returning_players(child, team):
            if pid in active or on > sim.league.ends_on:
                continue
            if next(p for p in child.projection(on).players if p.player.id == pid).probability == 0:
                continue
            if len(active) >= capacity:
                pending.append(pid)
                continue
            planned.append(
                InjuryReturn(player_id=pid, effective_on=on, drop=None, estimated=estimated)
            )
            active = (*active, pid)
        child.injury_plan_cache[team.id, tuple(sorted(team.players))] = tuple(planned)
    return child, tuple(sorted(set(pending)))


def return_plan(
    sim: Simulation,
    team_id: str,
    roster: tuple[str, ...],
) -> tuple[InjuryReturn, ...]:
    # Evaluate each lawful drop with the existing ROS engine and shared draws.
    # Multiple returns are handled chronologically; this does not assert a joint
    # optimum across all return/drop dates. Unknown return dates stay in IL.
    from fba.inseason.season import season_value

    team = next(t for t in sim.snapshot.teams if t.id == team_id)
    planned: list[InjuryReturn] = []
    capacity = len(sim.league.starter_slots) + sim.league.bench_slots
    active = roster
    transitions = list(sim.transitions.get(team_id, ()))
    returns = returning_players(sim, team)
    protected: frozenset[str] = sim.untouchable if team_id == sim.snapshot.mine else frozenset()
    if returns and sim.transitions.get(team_id):
        raise DataError(
            "injury_return: combined future add/drop and IL activation "
            "cannot yet be evaluated as one legal transition timeline"
        )
    for on, pid, estimated in returns:
        if pid in active or on > sim.league.ends_on:
            continue
        projection = {p.player.id: p for p in sim.projection(on).players}
        if projection[pid].probability == 0:
            continue
        candidates: tuple[str | None, ...] = (None,)
        if len(active) >= capacity:
            movable = tuple(p for p in active if p not in protected)
            if not movable:
                raise DataError(
                    f"injury_return.{pid}: 名單已滿且沒有可釋出的未保護球員；"
                    "IL 回歸待處理，請調整保護設定或手動騰出名額後同步"
                )
            candidates = tuple(p for p in movable if not sim.locked(p, on))
            if not candidates:
                on += timedelta(days=1)
                candidates = movable
            if not candidates or on > sim.league.ends_on:
                raise DataError(f"injury_return.{pid}: no legal activation/drop date")
        best = None
        best_value = float("-inf")
        for drop in candidates:
            sim.check_limits()
            changed = tuple(p for p in active if p != drop) + (pid,)
            child = Simulation(
                sim.league,
                sim.params,
                sim.players,
                sim.priors,
                sim.ledger,
                sim.snapshot,
                sim.as_of,
                sim.params.season_simulations.value,
                untouchable=tuple(sim.untouchable),
            )
            child.project_injury_returns = False
            child.projections = sim.projections
            child.projection_profiles = sim.projection_profiles
            child.draws = sim.season().draws
            child.transitions = {**sim.transitions, team_id: tuple((*transitions, (on, changed)))}
            child.cancelled, child.deadline = sim.cancelled, sim.deadline
            value = season_value(child, team_id, after=on, include_playoffs=True)
            if value > best_value + sim.params.tolerance.value:
                best, best_value = (drop, changed), value
        if best is None:
            raise DataError(f"injury_return.{pid}: no legal scenario")
        drop, active = best
        transitions.append((on, active))
        planned.append(InjuryReturn(player_id=pid, effective_on=on, drop=drop, estimated=estimated))
    return tuple(planned)
