from collections.abc import Callable
from datetime import date, datetime, timedelta
from hashlib import sha256

import numpy as np

from fba.contracts.base import DataError
from fba.contracts.inseason import FreeAgent, InseasonPreferences, WeekForecast
from fba.contracts.inseason_results import AddPlan, RosterMove
from fba.formulas.categories import category_values
from fba.formulas.registry import evaluate
from fba.inseason.drop_candidates import prioritized_drops
from fba.inseason.forecast import category_changes
from fba.inseason.matchup import Simulation
from fba.inseason.season import MissingSeasonOpponent, season_value


def legal_roster(sim: Simulation, roster: tuple[str, ...], on: date) -> bool:
    capacity = len(sim.league.starter_slots) + sim.league.bench_slots
    if len(roster) > capacity or len(set(roster)) != len(roster):
        return False
    positions = {p.player.id: p.player.positions for p in sim.projection(on).players}
    if set(roster) - positions.keys():
        return False
    return True  # Yahoo permits unfilled starter slots on an otherwise valid roster.


def earliest_move(sim: Simulation) -> date:
    now = sim.as_of.astimezone(sim.zone)
    delay = (
        sim.league.effective == "next_day"
        or now.time().replace(tzinfo=None) >= sim.league.cutoff_local_time
    )
    return now.date() + timedelta(days=int(delay))


def changed_simulation(sim: Simulation, team: str, moves: tuple[RosterMove, ...]) -> Simulation:
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
    child.projections, child.draws = sim.projections, sim.draws
    child.projection_profiles = sim.projection_profiles
    child.priority_cache = sim.priority_cache
    child.project_injury_returns = sim.project_injury_returns
    child.cancelled = sim.cancelled
    child.deadline = sim.deadline
    if sim.season_engine is not None:
        # Reuse the existing ROS pool without mixing it with weekly draws.
        # Forecast/lineup caches remain private to this transition scenario.
        child.season().draws = sim.season_engine.draws
    roster = sim.roster(team)
    transitions: list[tuple[date, tuple[str, ...]]] = []
    for move in moves:
        roster = sim.projected_roster(team, move.effective_on, roster)
        if move.drop not in roster or move.add in roster:
            raise DataError("recommendations: plan reuses an unavailable player")
        roster = tuple(sorted((*tuple(p for p in roster if p != move.drop), move.add)))
        transitions.append((move.effective_on, roster))
    child.transitions[team] = tuple(transitions)
    return child


def quick_score(
    sim: Simulation, add: str, drop: str, on: date, end: date, key_categories: tuple[str, ...]
) -> float:
    projection = {p.player.id: p for p in sim.projection(on).players}
    deltas: dict[str, float] = {}
    for pid, sign in ((add, 1), (drop, -1)):
        p = projection[pid]
        games = sum(
            len(sim.games_on(p.player.team_id, on + timedelta(days=day)))
            for day in range((end - on).days + 1)
        )
        for stat, value in p.expected.items():
            deltas[stat] = deltas.get(stat, 0.0) + sign * games * value
    week = next(w for w in sim.league.matchups if w.start <= on <= w.end)
    pair = next(
        p
        for p in sim.snapshot.pairings
        if p.week_id == week.id and sim.snapshot.mine in (p.home, p.away)
    )
    opponent = pair.away if pair.home == sim.snapshot.mine else pair.home
    a, _ = sim.total(sim.snapshot.mine, week.id)
    b, _ = sim.total(opponent, week.id)
    shifted = a.mean(axis=0) + np.array([deltas.get(s, 0.0) for s in sim.axes])
    shifted = np.maximum(shifted, 0.0)
    total = 0.0
    for category in sim.league.categories:
        if category.id not in key_categories:
            continue
        own = category_values(a, (category,), sim.axes)[:, 0]
        rival = category_values(b, (category,), sim.axes)[:, 0]
        mean_after = float(category_values(shifted, (category,), sim.axes)[0])
        common = dict(
            away=float(rival.mean()),
            home_variance=float(own.var()),
            away_variance=float(rival.var()),
            limit=1 / sim.params.tolerance.value,
        )
        before_z = evaluate("z", home=float(own.mean()), **common)
        after_z = evaluate("z", home=mean_after, **common)
        total += (
            evaluate("normal", z=after_z.result).result
            - evaluate("normal", z=before_z.result).result
        )
    return total


def search_adds(
    sim: Simulation,
    preferences: InseasonPreferences,
    week_id: str,
    progress: Callable[[float], None],
) -> tuple[AddPlan, ...]:
    with sim.budget("recommendations"):
        return search_add_plans(sim, preferences, week_id, progress)


def search_add_plans(
    sim: Simulation,
    preferences: InseasonPreferences,
    week_id: str,
    progress: Callable[[float], None],
) -> tuple[AddPlan, ...]:
    team = next(t for t in sim.snapshot.teams if t.id == sim.snapshot.mine)
    if team.adds_used is None:
        raise DataError(
            "adds_used: Yahoo transaction history is incomplete; enter the verified used-add count"
        )
    remaining = max(0, sim.league.adds_per_week - team.adds_used - preferences.reserve_adds)
    if remaining == 0:
        return ()
    pair = next(
        p for p in sim.snapshot.pairings if p.week_id == week_id and team.id in (p.home, p.away)
    )
    opponent = pair.away if pair.home == team.id else pair.home
    week = next(w for w in sim.league.matchups if w.id == week_id)
    before = sim.week(team.id, opponent, week_id)
    keys = tuple(c.id for c in before.categories if c.strategy == "key")
    if (before.priority and before.priority.status == "must_win") or not keys:
        keys = tuple(c.id for c in before.categories)
    future = (
        0.0
        if before.priority and before.priority.status == "must_win"
        else season_value(sim, team.id, after=week.end + timedelta(days=1))
    )
    beam: list[tuple[tuple[RosterMove, ...], tuple[str, ...]]] = [((), team.players)]
    results: list[AddPlan] = []
    for depth in range(remaining):
        candidates: list[tuple[float, tuple[RosterMove, ...]]] = []
        for moves, roster in beam:
            on = max(earliest_move(sim), moves[-1].effective_on if moves else week.start)
            while on <= week.end:
                candidates.extend(
                    candidate_moves(sim, preferences, roster, moves, on, week.end, keys)
                )
                on += timedelta(days=1)
        candidates.sort(
            key=lambda row: (
                -round(row[0] / sim.params.tolerance.value),
                tuple((m.add, m.drop, m.effective_on) for m in row[1]),
            )
        )
        evaluated: list[AddPlan] = []
        for index, (_, moves) in enumerate(candidates[: sim.params.shortlist.value]):
            sim.check_limits()
            plan = evaluate_plan(sim, preferences, moves, before, future)
            evaluated.append(plan)
            progress(
                (depth + (index + 1) / max(1, min(len(candidates), sim.params.shortlist.value)))
                / remaining
            )
        evaluated.sort(key=lambda p: (-round(p.score / sim.params.tolerance.value), p.id))
        results.extend(p for p in evaluated if admissible_plan(p, sim.params.tolerance.value))
        beam = [
            (p.moves, changed_simulation(sim, team.id, p.moves).transitions[team.id][-1][1])
            for p in evaluated[: sim.params.beam_width.value]
        ]
    unique = {p.id: p for p in results}
    return tuple(
        sorted(unique.values(), key=lambda p: (-round(p.score / sim.params.tolerance.value), p.id))
    )


def evaluate_plan(
    sim: Simulation,
    preferences: InseasonPreferences,
    moves: tuple[RosterMove, ...],
    before: WeekForecast,
    future: float,
) -> AddPlan:
    """Shared full evaluation for interactive search and historical recall checks."""
    team, opponent, week_id = before.home, before.away, before.week_id
    week = next(w for w in sim.league.matchups if w.id == week_id)
    child = changed_simulation(sim, team, moves)
    after = child.week(team, opponent, week_id)
    delta_week = evaluate("difference", after=after.score, before=before.score)
    must_win = before.priority is not None and before.priority.status == "must_win"
    delta_season = delta_strength = None
    unavailable = None
    if must_win:
        score = delta_week
    else:
        start = week.end + timedelta(days=1)
        future_sim, rosters = child, None
        if (
            moves
            and not any(t.injury_players for t in sim.snapshot.teams)
            and all(m.effective_on < start for m in moves)
        ):
            # After every move has taken effect, only the final active roster
            # matters. The existing ROS engine owns those roster-keyed caches.
            # IL scenarios and later transitions keep their full timeline.
            future_sim = sim
            rosters = {team: child.transitions[team][-1][1]} if moves else None
        delta_season = evaluate(
            "difference", after=season_value(future_sim, team, rosters, after=start), before=future
        )
        try:
            delta_strength = evaluate(
                "difference",
                after=season_value(future_sim, team, rosters, after=start, include_playoffs=True),
                before=season_value(sim, team, after=start, include_playoffs=True),
            )
        except MissingSeasonOpponent as exc:
            unavailable = str(exc)
        score = evaluate(
            "add_score",
            delta_week=delta_week.result,
            delta_season=delta_season.result,
            weight=preferences.future_weight,
        )
    traced_moves = tuple(
        m.model_copy(
            update={
                "starter_games": sum(
                    m.add in day.slots.values()
                    for day in after.lineups
                    if day.team_id == team
                    and day.on >= m.effective_on
                    and not any(
                        later.drop == m.add and later.effective_on <= day.on
                        for later in moves[index + 1 :]
                    )
                )
            }
        )
        for index, m in enumerate(moves)
    )
    identifier = sha256(
        str(tuple((m.add, m.drop, m.effective_on) for m in moves)).encode()
    ).hexdigest()
    return AddPlan(
        priority=before.priority,
        category_changes=category_changes((before,), (after,)),
        id=identifier,
        moves=traced_moves,
        before=before,
        after=after,
        delta_week=delta_week.result,
        delta_season=delta_season.result if delta_season else None,
        delta_strength=delta_strength.result if delta_strength else None,
        strength_unavailable=unavailable,
        score=score.result,
        traces=(
            delta_week,
            *((delta_season,) if delta_season else ()),
            *((delta_strength,) if delta_strength else ()),
            score,
        ),
    )


def candidate_moves(
    sim: Simulation,
    prefs: InseasonPreferences,
    roster: tuple[str, ...],
    moves: tuple[RosterMove, ...],
    on: date,
    end: date,
    keys: tuple[str, ...],
    *,
    exhaustive: bool = False,
) -> list[tuple[float, tuple[RosterMove, ...]]]:
    sim.check_limits()
    result: list[tuple[float, tuple[RosterMove, ...]]] = []
    available = {free.player_id: free for free in sim.snapshot.free_agents}
    for move in moves:
        available.pop(move.add, None)
        available[move.drop] = FreeAgent(
            player_id=move.drop,
            status="waiver",
            clears_at=datetime.combine(
                move.effective_on + timedelta(days=sim.league.waiver_days),
                datetime.min.time(),
                sim.zone,
            ),
        )
    current = changed_simulation(sim, sim.snapshot.mine, moves) if moves else sim
    roster = current.projected_roster(sim.snapshot.mine, on, roster)
    drops = prioritized_drops(
        current,
        prefs,
        roster,
        tuple(m.add for m in moves if m.effective_on == on),
        on,
        exhaustive=exhaustive,
    )
    for free in available.values():
        if free.player_id in roster:
            continue
        effective = datetime.combine(on, sim.league.cutoff_local_time, sim.zone)
        if free.status == "waiver" and (free.clears_at is None or free.clears_at > effective):
            continue
        for drop in drops:
            sim.check_limits()
            if drop in prefs.untouchable:
                continue
            if any(m.add == drop and m.effective_on == on for m in moves):
                continue  # Same-day acquire/release cannot add a counted game.
            team = next(t for t in sim.snapshot.teams if t.id == sim.snapshot.mine)
            if drop in team.selected_slots.values() and sim.locked(drop, on):
                continue
            proposed = tuple(p for p in roster if p != drop) + (free.player_id,)
            if legal_roster(sim, proposed, on):
                move = RosterMove(add=free.player_id, drop=drop, effective_on=on, starter_games=0)
                result.append(
                    (quick_score(current, free.player_id, drop, on, end, keys), (*moves, move))
                )
    return result


def admissible_plan(plan: AddPlan, tolerance: float) -> bool:
    if plan.priority is not None and plan.priority.status == "must_win":
        return plan.delta_week > tolerance
    return (
        plan.delta_strength is not None
        and plan.delta_strength >= -tolerance
        and plan.score > tolerance
    )
