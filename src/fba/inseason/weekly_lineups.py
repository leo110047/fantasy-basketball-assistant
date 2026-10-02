"""Complete conditional weekly search with bounded batches and certified pruning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from itertools import islice, product
from math import prod
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from fba.contracts.config import Linear
from fba.contracts.inseason import DayLineup
from fba.formulas.categories import sample_scores
from fba.formulas.simulation import mean_array, outward_interval
from fba.inseason.lineup_bounds import score_ceiling
from fba.inseason.lineup_space import cached_subsets
from fba.inseason.weekly_players import PlayerSearch

if TYPE_CHECKING:
    from fba.inseason.matchup import Simulation

type Array = NDArray[np.float64]
type Assignment = dict[str, str]
type LineupKey = tuple[int, tuple[tuple[str, ...], ...]]


@dataclass
class DayChoices:
    roster: tuple[str, ...]
    draws: dict[str, Array]
    rows: tuple[Assignment, ...]
    bounds: tuple[tuple[Array, Array], ...]


def day_choices(
    sim: Simulation, team: str, day: DayLineup, roster: tuple[str, ...], through: datetime
) -> DayChoices:
    sim.check_limits()
    active = sim.projected_roster(team, day.on, roster)
    draws = sim.daily_draws(active, day.on, max(sim.as_of, through))
    fixed, slots, free = sim.lineup_constraints(team, day.on, draws)
    ids = tuple(sorted(free))
    eligible = tuple(
        tuple(bool(set(free[p]).intersection(s.eligible_positions)) for s in slots) for p in ids
    )
    legal = cached_subsets(eligible, len(slots))
    sim.check_limits()
    rows = tuple(
        {**fixed, **{slots[s].id: ids[p] for s, p in enumerate(assigned) if p is not None}}
        for _, assigned in legal
    )
    rows = tuple(sorted(rows, key=lambda row: (-len(row), tuple(sorted(row.values())))))
    zero = np.zeros((sim.samples, len(sim.axes)))
    bounds = [(draws[p], draws[p]) for p in fixed.values()]
    for slot in slots:
        low, high = zero.copy(), zero.copy()
        for pid in ids:
            if set(free[pid]).intersection(slot.eligible_positions):
                low = np.minimum(low, draws[pid])
                high = np.maximum(high, draws[pid])
        # Relax uniqueness and allow an empty slot. Every legal assignment is
        # inside these per-slot bounds, in the same floating-point sum order.
        bounds.append((low, high))
    return DayChoices(active, draws, rows, tuple(bounds))


def lineup_key(rows: tuple[Assignment, ...]) -> LineupKey:
    return -sum(len(row) for row in rows), tuple(tuple(sorted(row.values())) for row in rows)


def add_assignment(total: Array, day: DayChoices, row: Assignment) -> Array:
    return sum((day.draws[pid] for pid in row.values()), start=total)


def completion_ceiling(
    sim: Simulation,
    prefix: Array,
    remaining: tuple[DayChoices, ...],
    actual: Array,
    opponent: Array,
) -> float:
    lower, upper = prefix, prefix
    for day in remaining:
        sim.check_limits()
        for low, high in day.bounds:
            lower, upper = outward_interval({"lower": lower + low, "upper": upper + high})
    lower, upper = outward_interval({"lower": lower + actual, "upper": upper + actual})
    return score_ceiling(sim, lower, upper, opponent)


@dataclass
class WeeklySearch:
    sim: Simulation
    days: tuple[DayChoices, ...]
    actual: Array
    opponent: Array
    preferred: tuple[Assignment, ...] = field(init=False)
    can_prune: bool = field(init=False)
    best: tuple[Assignment, ...] = ()
    best_total: Array = field(init=False)
    best_value: float = float("-inf")
    best_key: LineupKey | None = None

    def __post_init__(self) -> None:
        self.preferred = tuple(
            min(day.rows, key=lambda row: (-len(row), tuple(sorted(row.values()))))
            for day in self.days
        )
        self.can_prune = all(
            isinstance(c.formula, Linear) or c.formula.zero_denominator != "error"
            for c in self.sim.league.categories
        )
        self.best_total = self.actual

    def run(self) -> tuple[Array, tuple[Assignment, ...]]:
        zero = np.zeros_like(self.actual)
        self.consider((), zero, (self.preferred,))
        players = PlayerSearch.create(self) if self.can_prune else None
        if players is None:
            self.visit((), zero)
        elif not self.pruned(
            completion_ceiling(self.sim, zero, self.days, self.actual, self.opponent),
            self.preferred,
        ):
            players.run()
        self.sim.check_limits()
        return self.best_total, self.best

    def pruned(self, ceiling: float, possible: tuple[Assignment, ...]) -> bool:
        return ceiling < self.best_value - self.sim.params.tolerance.value or (
            ceiling <= self.best_value + self.sim.params.tolerance.value
            and self.best_key is not None
            and lineup_key(possible) >= self.best_key
        )

    def consider(
        self,
        prefix: tuple[Assignment, ...],
        total: Array,
        tails: tuple[tuple[Assignment, ...], ...],
    ) -> None:
        self.sim.check_limits()
        totals: list[Array] = []
        for tail in tails:
            candidate = total
            for day, row in zip(self.days[len(prefix) :], tail, strict=True):
                candidate = add_assignment(candidate, day, row)
            totals.append(self.actual + candidate)
        raw = mean_array(
            sample_scores(
                np.stack(totals),
                self.opponent,
                self.sim.league.categories,
                self.sim.axes,
                self.sim.league.scoring,
                self.sim.league.category_ties,
                0.0,
            ),
            axis=-1,
        )
        for tail, candidate, score in zip(tails, totals, raw, strict=True):
            value = self.sim.calibrated_score(float(score)).result
            rows = (*prefix, *tail)
            key = lineup_key(rows)
            if value > self.best_value + self.sim.params.tolerance.value or (
                abs(value - self.best_value) <= self.sim.params.tolerance.value
                and (self.best_key is None or key < self.best_key)
            ):
                self.best, self.best_total, self.best_value, self.best_key = (
                    rows,
                    candidate,
                    value,
                    key,
                )

    def visit(self, prefix: tuple[Assignment, ...], total: Array) -> None:
        self.sim.check_limits()
        remaining = self.days[len(prefix) :]
        if self.can_prune and self.best_key is not None:
            ceiling = completion_ceiling(self.sim, total, remaining, self.actual, self.opponent)
            possible_key = lineup_key((*prefix, *self.preferred[len(prefix) :]))
            if ceiling < self.best_value - self.sim.params.tolerance.value or (
                ceiling <= self.best_value + self.sim.params.tolerance.value
                and possible_key >= self.best_key
            ):
                return
        if (
            prod(len(day.rows) for day in remaining)
            <= self.sim.params.weekly_exact_candidates.value
        ):
            candidates = product(*(day.rows for day in remaining))
            while chunk := tuple(islice(candidates, self.sim.params.lineup_batch.value)):
                self.consider(prefix, total, chunk)
            return
        day = remaining[0]
        for row in day.rows:
            self.visit((*prefix, row), add_assignment(total, day, row))


def joint_lineup(
    sim: Simulation,
    team: str,
    week: str,
    opponent: Array,
    roster: tuple[str, ...] | None,
    initial: tuple[DayLineup, ...],
    through: datetime,
) -> tuple[Array, tuple[DayLineup, ...]]:
    days = tuple(
        day_choices(sim, team, day, roster if roster is not None else sim.roster(team), through)
        for day in initial
    )
    actual, _ = sim.actual(team, week)
    best_total, best = WeeklySearch(sim, days, actual, opponent).run()
    sim.joint_weeks.add(
        (team, week, tuple(sorted(roster if roster is not None else sim.roster(team))))
    )
    return best_total, tuple(
        day.model_copy(
            update={
                "slots": best[i],
                "bench": tuple(p for p in days[i].roster if p not in best[i].values()),
            }
        )
        for i, day in enumerate(initial)
    )
