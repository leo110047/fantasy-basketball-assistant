"""Bounded exhaustive reference search for offline validation, never live advice."""

from datetime import timedelta

from fba.contracts.inseason import CalculationTimeout, InseasonPreferences, WeekForecast
from fba.contracts.inseason_results import RosterMove
from fba.inseason.matchup import Simulation
from fba.inseason.recommendations import (
    candidate_moves,
    changed_simulation,
    earliest_move,
    evaluate_plan,
)


def exhaustive_plans(
    sim: Simulation,
    preferences: InseasonPreferences,
    before: WeekForecast,
    future: float,
    remaining: int,
    maximum: int,
    selected_score: float,
) -> tuple[int, bool | None]:
    week = next(w for w in sim.league.matchups if w.id == before.week_id)
    frontier: list[tuple[RosterMove, ...]] = [()]
    best = 0.0  # Doing nothing is a legitimate policy.
    count = 0
    for _ in range(remaining):
        following: list[tuple[RosterMove, ...]] = []
        for moves in frontier:
            roster = (
                changed_simulation(sim, before.home, moves).transitions[before.home][-1][1]
                if moves
                else sim.roster(before.home)
            )
            on = max(earliest_move(sim), moves[-1].effective_on if moves else week.start)
            while on <= week.end:
                # No z screening and no beam truncation in the reference search.
                for _, candidate in candidate_moves(
                    sim, preferences, roster, moves, on, week.end, ()
                ):
                    count += 1
                    if count > maximum:
                        raise CalculationTimeout(
                            "replay.oracle_max_plans: exhaustive search exceeded configured limit; "
                            "no recall result can be certified"
                        )
                    plan = evaluate_plan(sim, preferences, candidate, before, future)
                    best = max(best, plan.score)
                    following.append(candidate)
                on += timedelta(days=1)
        frontier = following
    return count, (
        round(selected_score / sim.params.tolerance.value)
        >= round(best / sim.params.tolerance.value)
        if count
        else None
    )
