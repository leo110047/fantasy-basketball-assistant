from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from hashlib import sha256

import numpy as np

from fba.contracts.base import DataError
from fba.contracts.inseason import FreeAgent, InseasonPreferences, WeekForecast
from fba.contracts.inseason_results import AddPlan, RosterMove
from fba.formulas.categories import category_values
from fba.formulas.registry import evaluate
from fba.formulas.simulation import mean_array, nonnegative_samples, variance_array
from fba.inseason.drop_candidates import prioritized_drops
from fba.inseason.forecast import category_changes
from fba.inseason.matchup import Simulation
from fba.inseason.roster_timeline import RosterChange
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
    if any(a.effective_on > b.effective_on for a, b in zip(moves, moves[1:], strict=False)):
        raise DataError("recommendations: moves must be in chronological order")
    child.transitions = sim.transitions.copy()
    child.transitions[team] = tuple(RosterChange(m.effective_on, m.add, m.drop) for m in moves)
    return child


def quick_score(
    sim: Simulation, moves: tuple[RosterMove, ...], end: date, key_categories: tuple[str, ...]
) -> float:
    if not key_categories:
        return 0.0  # Exhaustive/reference policies do not need screening scores.
    week = next(w for w in sim.league.matchups if w.start <= moves[-1].effective_on <= w.end)
    pair = next(
        p
        for p in sim.snapshot.pairings
        if p.week_id == week.id and sim.snapshot.mine in (p.home, p.away)
    )
    opponent = pair.away if pair.home == sim.snapshot.mine else pair.home
    a, _ = sim.total(sim.snapshot.mine, week.id)
    b, _ = sim.total(opponent, week.id)
    # Compare every prefix against the same original team. This remains a raw
    # expected-game screening approximation; evaluate_plan owns legal lineups.
    deltas: dict[str, float] = {}
    for move in moves:
        projection = {p.player.id: p for p in sim.projection(move.effective_on).players}
        for pid, sign in ((move.add, 1), (move.drop, -1)):
            player = projection[pid]
            games = sum(
                len(sim.games_on(player.player.team_id, move.effective_on + timedelta(days=day)))
                for day in range((end - move.effective_on).days + 1)
            )
            for stat, value in player.expected.items():
                contribution = evaluate(
                    "product", gain=value, probability=float(sign * games)
                ).result
                deltas[stat] = deltas.get(stat, 0.0) + contribution
    shifted = mean_array(a, axis=0) + np.array([deltas.get(s, 0.0) for s in sim.axes])
    shifted = nonnegative_samples({"values": shifted})
    total = 0.0
    for category in sim.league.categories:
        if category.id not in key_categories:
            continue
        own = category_values(a, (category,), sim.axes)[:, 0]
        rival = category_values(b, (category,), sim.axes)[:, 0]
        mean_after = float(category_values(shifted, (category,), sim.axes)[0])
        common = dict(
            away=float(mean_array(rival)),
            home_variance=float(variance_array(own)),
            away_variance=float(variance_array(rival)),
            limit=1 / sim.params.tolerance.value,
        )
        before_z = evaluate("z", home=float(mean_array(own)), **common)
        after_z = evaluate("z", home=mean_after, **common)
        total += evaluate(
            "difference",
            after=evaluate("normal", z=after_z.result).result,
            before=evaluate("normal", z=before_z.result).result,
        ).result
    return total


def search_context(sim: Simulation, before: WeekForecast) -> tuple[tuple[str, ...], float]:
    """One objective/category policy for interactive and reference evaluation."""
    must_win = before.priority is not None and before.priority.status == "must_win"
    keys = tuple(c.id for c in before.categories if c.strategy == "key")
    if must_win or not keys:
        keys = tuple(c.id for c in before.categories)
    week = next(w for w in sim.league.matchups if w.id == before.week_id)
    future = 0.0 if must_win else season_value(sim, before.home, after=week.end + timedelta(days=1))
    return keys, future


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
    keys, future = search_context(sim, before)
    tolerance = sim.params.tolerance.value
    width = sim.params.shortlist.value

    def rank(plan: AddPlan) -> tuple[int, str]:
        return -round(plan.score / tolerance), plan.id

    def visit(moves: tuple[RosterMove, ...], start: float, stop: float) -> Iterator[AddPlan]:
        candidates = ordered_candidates(sim, preferences, moves, week.start, week.end, keys)
        for offset in range(0, len(candidates), width):
            # The former shortlist and beam now order bounded batches. Every
            # deferred sibling is visited, including inadmissible prefixes.
            batch = sorted(
                (
                    evaluate_plan(sim, preferences, candidate, before, future)
                    for _, candidate in candidates[offset : offset + width]
                ),
                key=rank,
            )
            yield from batch
            for beam_start in range(0, len(batch), sim.params.beam_width.value):
                for index, plan in enumerate(
                    batch[beam_start : beam_start + sim.params.beam_width.value], beam_start
                ):
                    sim.check_limits()
                    lower = start + (stop - start) * (offset + index) / len(candidates)
                    upper = (
                        stop
                        if offset + index + 1 == len(candidates)
                        else start + (stop - start) * (offset + index + 1) / len(candidates)
                    )
                    if len(plan.moves) < remaining:
                        yield from visit(plan.moves, lower, upper)
                    progress(upper)

    # Keep the best published choices per move count, not every forecast in
    # the exponentially growing tree. This never restricts continuation.
    results: dict[int, list[AddPlan]] = {}
    for plan in visit((), 0.0, 1.0):
        if admissible_plan(plan, tolerance):
            depth_results = results.setdefault(len(plan.moves), [])
            depth_results.append(plan)
            depth_results.sort(key=rank)
            del depth_results[width:]
    sim.check_limits()
    progress(1.0)
    return tuple(sorted((p for plans in results.values() for p in plans), key=rank))


def ordered_candidates(
    sim: Simulation,
    preferences: InseasonPreferences,
    moves: tuple[RosterMove, ...],
    start: date,
    end: date,
    keys: tuple[str, ...],
) -> list[tuple[float, tuple[RosterMove, ...]]]:
    """Low-contribution drops first, then z order; never remove legal moves."""
    roster = sim.roster(sim.snapshot.mine)
    candidates: list[tuple[float, tuple[RosterMove, ...]]] = []
    preferred: set[tuple[RosterMove, ...]] = set()
    on = max(earliest_move(sim), moves[-1].effective_on if moves else start)
    while on <= end:
        preferred.update(
            candidate
            for _, candidate in candidate_moves(sim, preferences, roster, moves, on, end, keys)
        )
        candidates.extend(
            candidate_moves(sim, preferences, roster, moves, on, end, keys, exhaustive=True)
        )
        on += timedelta(days=1)
    return sorted(
        candidates,
        key=lambda row: (
            row[1] not in preferred,
            -round(row[0] / sim.params.tolerance.value),
            tuple((m.add, m.drop, m.effective_on) for m in row[1]),
        ),
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
            rosters = {team: child.projected_roster(team, start, sim.roster(team))}
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
    roster = current.projected_roster(sim.snapshot.mine, on, sim.roster(sim.snapshot.mine))
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
                result.append((quick_score(sim, (*moves, move), end, keys), (*moves, move)))
    return result


def admissible_plan(plan: AddPlan, tolerance: float) -> bool:
    if plan.priority is not None and plan.priority.status == "must_win":
        return plan.delta_week > tolerance
    return (
        plan.delta_strength is not None
        and plan.delta_strength >= -tolerance
        and plan.score > tolerance
    )
