"""Conditional IL activation scenarios; no real Yahoo roster is changed."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from fba.contracts.inseason import InjuryReturn
from fba.inseason.matchup import Simulation
from fba.inseason.roster_timeline import (
    InvalidRosterChange,
    apply_change,
    merge_changes,
    roster_after,
)

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
    sim: Simulation,
    team_ids: tuple[str, ...],
    saved: dict[str, tuple[InjuryReturn, ...]] | None = None,
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
    if not missing and not saved:
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
    for team_id, recorded in (saved or {}).items():
        cache_saved_returns(child, team_id, recorded)
    capacity = len(sim.league.starter_slots) + sim.league.bench_slots
    for team in missing:
        if team.id in (saved or {}):
            continue
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


def cache_saved_returns(sim: Simulation, team_id: str, planned: tuple[InjuryReturn, ...]) -> None:
    """Validate a recorded F3 decision using current facts, without searching ROS."""
    team = next(t for t in sim.snapshot.teams if t.id == team_id)
    expected = {
        pid: (on, estimated)
        for on, pid, estimated in returning_players(sim, team)
        if pid not in team.players
        and on <= sim.league.ends_on
        and next(p for p in sim.projection(on).players if p.player.id == pid).probability > 0
    }
    if {m.player_id for m in planned} != set(expected) or len(planned) != len(expected):
        raise InvalidRosterChange("today.plan_id: IL 回歸資訊已變或計畫不完整，請重新計算 F3")
    if any(a.effective_on > b.effective_on for a, b in zip(planned, planned[1:], strict=False)):
        raise InvalidRosterChange("today.plan_id: IL 回歸順序不合法")
    protected: frozenset[str] = sim.untouchable if team_id == sim.snapshot.mine else frozenset()
    for move in planned:
        on, estimated = expected[move.player_id]
        if (
            move.estimated != estimated
            or not on <= move.effective_on <= sim.league.ends_on
            or move.drop in protected
            or (move.drop is not None and sim.locked(move.drop, move.effective_on))
        ):
            raise InvalidRosterChange(
                "today.plan_id: IL 回歸日期、保護或鎖定條件已變，請重新計算 F3"
            )
    capacity = len(sim.league.starter_slots) + sim.league.bench_slots
    active = team.players
    for change in merge_changes(sim.transitions.get(team_id, ()), planned):
        active = apply_change(active, change)
        if len(active) > capacity:
            raise InvalidRosterChange("today.plan_id: IL 與換人計畫超過名單容量")
    sim.injury_plan_cache[team_id, tuple(sorted(team.players))] = planned


def return_plan(
    sim: Simulation,
    team_id: str,
    roster: tuple[str, ...],
) -> tuple[InjuryReturn, ...]:
    # Optimize chronological releases against a complete legal suffix, including
    # every explicit move and later IL return. This remains a greedy IL policy,
    # not a claim of a joint optimum across all release dates.
    from fba.inseason.season import season_value

    planned = feasible_returns(sim, team_id, roster, {})
    fixed: dict[str, InjuryReturn] = {}
    for index in range(len(planned)):
        returning = planned[index]
        active = before_return(sim, team_id, roster, tuple(fixed.values()), returning.effective_on)
        best: tuple[InjuryReturn, ...] | None = None
        best_value = float("-inf")
        for drop in release_options(sim, team_id, active, returning.effective_on):
            sim.check_limits()
            forced = {**fixed, returning.player_id: returning.model_copy(update={"drop": drop})}
            try:
                trial = feasible_returns(sim, team_id, roster, forced)
            except InvalidRosterChange:
                continue  # A future explicit move or activation would be invalidated.
            child = return_scenario(sim, team_id, trial)
            value = season_value(
                child,
                team_id,
                {team_id: roster},
                after=returning.effective_on,
                include_playoffs=True,
            )
            if value > best_value + sim.params.tolerance.value:
                best, best_value = trial, value
        if best is None:
            raise InvalidRosterChange(f"injury_return.{returning.player_id}: no legal scenario")
        planned = best
        fixed[returning.player_id] = planned[index]
    return planned


def release_options(
    sim: Simulation, team: str, active: tuple[str, ...], on: date
) -> tuple[str | None, ...]:
    capacity = len(sim.league.starter_slots) + sim.league.bench_slots
    if len(active) < capacity:
        return (None,)
    protected: frozenset[str] = sim.untouchable if team == sim.snapshot.mine else frozenset()
    movable = tuple(sorted(p for p in active if p not in protected))
    if not movable:
        raise InvalidRosterChange(
            "injury_return: 名單已滿且沒有可釋出的未保護球員；"
            "IL 回歸待處理，請調整保護設定或手動騰出名額後同步"
        )
    return tuple(p for p in movable if not sim.locked(p, on))


def before_return(
    sim: Simulation, team: str, roster: tuple[str, ...], planned: tuple[InjuryReturn, ...], on: date
) -> tuple[str, ...]:
    earlier = tuple(m for m in sim.transitions.get(team, ()) if m.effective_on < on)
    return roster_after(roster, merge_changes(earlier, planned), on)


def feasible_returns(
    sim: Simulation, team_id: str, roster: tuple[str, ...], forced: dict[str, InjuryReturn]
) -> tuple[InjuryReturn, ...]:
    team = next(t for t in sim.snapshot.teams if t.id == team_id)
    returns = tuple(
        row
        for row in returning_players(sim, team)
        if row[1] not in roster
        and row[0] <= sim.league.ends_on
        and next(p for p in sim.projection(row[0]).players if p.player.id == row[1]).probability > 0
    )
    return legal_return_suffix(sim, team_id, roster, returns, forced, ())


def legal_return_suffix(
    sim: Simulation,
    team: str,
    roster: tuple[str, ...],
    returns: tuple[tuple[date, str, bool], ...],
    forced: dict[str, InjuryReturn],
    planned: tuple[InjuryReturn, ...],
) -> tuple[InjuryReturn, ...]:
    """Find a complete legal suffix, including reacquisitions of released players."""
    sim.check_limits()
    if not returns:
        roster_after(
            roster, merge_changes(sim.transitions.get(team, ()), planned), sim.league.ends_on
        )
        return planned
    on, pid, estimated = returns[0]
    on = max(on, planned[-1].effective_on if planned else on)
    if pid in forced:
        if forced[pid].effective_on < on:
            raise InvalidRosterChange(f"injury_return.{pid}: forced date precedes earlier return")
        on = forced[pid].effective_on
    active = before_return(sim, team, roster, planned, on)
    options = release_options(sim, team, active, on)
    if not options and pid not in forced and any(sim.locked(p, on) for p in active):
        on += timedelta(days=1)
        active = before_return(sim, team, roster, planned, on)
        options = release_options(sim, team, active, on)
    if not options or on > sim.league.ends_on:
        raise InvalidRosterChange(f"injury_return.{pid}: no legal activation/drop date")
    candidates = (forced[pid].drop,) if pid in forced else options
    failure: InvalidRosterChange | None = None
    for drop in candidates:
        if drop not in options:
            continue
        move = InjuryReturn(player_id=pid, effective_on=on, drop=drop, estimated=estimated)
        try:
            return legal_return_suffix(sim, team, roster, returns[1:], forced, (*planned, move))
        except InvalidRosterChange as exc:
            if failure is None:
                failure = exc
    raise failure or InvalidRosterChange(
        f"injury_return.{pid}: release conflicts with the future plan"
    )


def return_scenario(sim: Simulation, team: str, planned: tuple[InjuryReturn, ...]) -> Simulation:
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
    child.projections, child.projection_profiles, child.draws = (
        sim.projections,
        sim.projection_profiles,
        sim.season().draws,
    )
    child.transitions = {
        **sim.transitions,
        team: merge_changes(sim.transitions.get(team, ()), planned),
    }
    child.cancelled, child.deadline = sim.cancelled, sim.deadline
    return child
