"""Joint enumeration only when the entire legal weekly space fits its budget."""

from __future__ import annotations

from datetime import datetime
from itertools import product
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from fba.contracts.inseason import DayLineup
from fba.formulas.categories import sample_scores
from fba.formulas.simulation import mean_array
from fba.inseason.lineup_space import cached_subsets

if TYPE_CHECKING:
    from fba.inseason.matchup import Simulation


def joint_lineup(
    sim: Simulation,
    team: str,
    week: str,
    opponent: NDArray[np.float64],
    roster: tuple[str, ...] | None,
    initial: tuple[DayLineup, ...],
    through: datetime,
) -> tuple[NDArray[np.float64], tuple[DayLineup, ...]] | None:
    choices: list[tuple[dict[str, str], ...]] = []
    daily: list[dict[str, NDArray[np.float64]]] = []
    active_rosters: list[tuple[str, ...]] = []
    count = 1
    for day in initial:
        active = roster if roster is not None else sim.roster(team)
        active = sim.projected_roster(team, day.on, active)
        draws = sim.daily_draws(active, day.on, max(sim.as_of, through))
        fixed, slots, free = sim.lineup_constraints(team, day.on, draws)
        ids = tuple(sorted(free))
        eligible = tuple(
            tuple(bool(set(free[p]).intersection(s.eligible_positions)) for s in slots) for p in ids
        )
        legal = cached_subsets(eligible, len(slots))
        count *= len(legal)
        if count > sim.params.weekly_exact_candidates.value:
            return None
        choices.append(
            tuple(
                {**fixed, **{slots[s].id: ids[p] for s, p in enumerate(assigned) if p is not None}}
                for _, assigned in legal
            )
        )
        daily.append(draws)
        active_rosters.append(active)
    actual, _ = sim.actual(team, week)
    best = None
    best_total = actual
    best_value = float("-inf")
    best_key = None
    for assignments in product(*choices):
        sim.check_limits()
        total = actual + sum(
            (daily[i][pid] for i, row in enumerate(assignments) for pid in row.values()),
            start=np.zeros_like(actual),
        )
        raw = sample_scores(
            total,
            opponent,
            sim.league.categories,
            sim.axes,
            sim.league.scoring,
            sim.league.category_ties,
            0.0,
        )
        value = sim.calibrated_score(float(mean_array(raw))).result
        key = (
            -sum(len(row) for row in assignments),
            tuple(tuple(sorted(row.values())) for row in assignments),
        )
        if value > best_value + sim.params.tolerance.value or (
            abs(value - best_value) <= sim.params.tolerance.value
            and (best_key is None or key < best_key)
        ):
            best, best_total, best_value, best_key = assignments, total, value, key
    if best is None:
        return None
    sim.joint_weeks.add((team, week, tuple(sorted(roster or sim.roster(team)))))
    return best_total, tuple(
        day.model_copy(
            update={
                "slots": best[i],
                "bench": tuple(p for p in active_rosters[i] if p not in best[i].values()),
            }
        )
        for i, day in enumerate(initial)
    )
