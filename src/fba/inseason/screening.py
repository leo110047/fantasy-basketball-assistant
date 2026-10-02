"""One baseline and frozen moments per candidate batch; screening only orders search."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

import numpy as np

from fba.contracts.config import Category
from fba.formulas.categories import category_values
from fba.formulas.registry import evaluate
from fba.formulas.simulation import mean_array, nonnegative_samples, variance_array

if TYPE_CHECKING:
    from fba.contracts.inseason_results import RosterMove
    from fba.inseason.matchup import Array, Simulation


@dataclass
class Screening:
    sim: Simulation
    end: date
    baseline: Array
    categories: tuple[Category, ...]
    moments: tuple[dict[str, float], ...]
    before: tuple[float, ...]
    contributions: dict[tuple[str, date, int], dict[str, float]] = field(default_factory=dict)

    @classmethod
    def create(cls, sim: Simulation, end: date, keys: tuple[str, ...]) -> Screening:
        week = next(w for w in sim.league.matchups if w.start <= end <= w.end)
        pair = next(
            p
            for p in sim.snapshot.pairings
            if p.week_id == week.id and sim.snapshot.mine in (p.home, p.away)
        )
        opponent = pair.away if pair.home == sim.snapshot.mine else pair.home
        a, _ = sim.total(sim.snapshot.mine, week.id)
        b, _ = sim.total(opponent, week.id)
        categories = tuple(c for c in sim.league.categories if c.id in keys)
        moments: list[dict[str, float]] = []
        before: list[float] = []
        for category in categories:
            own = category_values(a, (category,), sim.axes)[:, 0]
            rival = category_values(b, (category,), sim.axes)[:, 0]
            common = dict(
                away=float(mean_array(rival)),
                home_variance=float(variance_array(own)),
                away_variance=float(variance_array(rival)),
                limit=1 / sim.params.tolerance.value,
            )
            z = evaluate("z", home=float(mean_array(own)), **common)
            moments.append(common)
            before.append(evaluate("normal", z=z.result).result)
        return cls(sim, end, mean_array(a, axis=0), categories, tuple(moments), tuple(before))

    def contribution(self, pid: str, on: date, sign: int) -> dict[str, float]:
        key = pid, on, sign
        if key not in self.contributions:
            player = self.sim.player_index.get(self.sim.projection(on))[pid]
            games = sum(
                len(self.sim.games_on(player.player.team_id, on + timedelta(days=day)))
                for day in range((self.end - on).days + 1)
            )
            self.contributions[key] = {
                stat: evaluate("product", gain=value, probability=float(sign * games)).result
                for stat, value in player.expected.items()
            }
        return self.contributions[key]

    def score(self, moves: tuple[RosterMove, ...]) -> float:
        self.sim.check_limits()
        deltas: dict[str, float] = {}
        for move in moves:
            for pid, sign in ((move.add, 1), (move.drop, -1)):
                for stat, value in self.contribution(pid, move.effective_on, sign).items():
                    deltas[stat] = deltas.get(stat, 0.0) + value
        shifted = nonnegative_samples(
            {"values": self.baseline + np.array([deltas.get(s, 0.0) for s in self.sim.axes])}
        )
        values = category_values(shifted, self.categories, self.sim.axes)
        total = 0.0
        for value, common, before in zip(values, self.moments, self.before, strict=True):
            after = evaluate("z", home=float(value), **common)
            total += evaluate(
                "difference", after=evaluate("normal", z=after.result).result, before=before
            ).result
        return total
