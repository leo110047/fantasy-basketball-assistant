from math import fsum
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import Bounds, LinearConstraint, milp

from fba.contracts.auction import (
    AuctionPlayer,
    CalculationTimeout,
    CapCalculation,
    Infeasible,
    Plan,
    SolverError,
)
from fba.contracts.config import LeagueRules, SolverParameters
from fba.core.roster import assign, capacity, hall_constraints
from fba.formulas.registry import evaluate


class Portfolio:
    """An invocation-local exact solver; no I/O or shared state."""

    def __init__(
        self,
        league: LeagueRules,
        parameters: SolverParameters,
        players: tuple[AuctionPlayer, ...],
        costs: tuple[int | None, ...],
        available: tuple[bool, ...],
        owned: tuple[str, ...],
        budget: int,
        *,
        prune: bool = True,
    ) -> None:
        self.league = league
        self.parameters = parameters
        self.players = players
        self.costs = costs
        self.available = available
        self.owned = owned
        self.budget = budget
        self.prune = prune
        self.calls = 0
        self.slots = capacity(league) - len(owned)
        self.values = np.array([p.utility or 0.0 for p in players])
        cost = np.array([c if c is not None else np.inf for c in costs])
        positions = tuple(frozenset(p.positions) for p in players)
        supersets = np.array([[p <= q for q in positions] for p in positions])
        weak = (self.values[None, :] >= self.values[:, None]) & (cost[None, :] <= cost[:, None])
        earlier = np.arange(len(players))[None, :] < np.arange(len(players))[:, None]
        strict = (self.values[None, :] > self.values[:, None]) & (cost[None, :] < cost[:, None])
        self.canonical_dominance = (
            supersets & weak & (earlier | (self.values[None, :] > self.values[:, None]))
        )
        self.dominance = (
            supersets
            & weak
            & (
                earlier
                | strict
                | (self.values[None, :] > self.values[:, None])
                | (cost[None, :] < cost[:, None])
            )
        )
        rows = hall_constraints(league)
        self.hall = np.array(
            [[bool(union.intersection(p.positions)) for p in players] for union, _ in rows],
            dtype=float,
        )
        fixed = [i for i, p in enumerate(players) if p.id in owned]
        self.need = np.array([n for _, n in rows], dtype=float) - self.hall[:, fixed].sum(axis=1)

    def candidates(self, excluded: tuple[int, ...], slots: int, canonical: bool) -> list[int]:
        candidates = [
            i for i, allowed in enumerate(self.available) if allowed and i not in excluded
        ]
        if not self.prune:
            return candidates
        dominance = self.canonical_dominance if canonical else self.dominance
        counts = dominance[np.ix_(candidates, candidates)].sum(axis=1)
        return [i for i, count in zip(candidates, counts, strict=True) if count < slots]

    def solve(
        self,
        *,
        exclude: int | None = None,
        force: int | None = None,
        target: float | None = None,
        objective: Literal["value", "cost"] = "value",
        canonical: bool = False,
    ) -> Plan | Infeasible:
        fixed = () if force is None else (force,)
        slots = self.slots - len(fixed)
        fixed_cost = self.league.minimum_bid if force is not None else 0
        if slots < 0 or self.budget < fixed_cost:
            return Infeasible(reason="roster or budget cannot accommodate the forced player")
        candidates = self.candidates(
            (*fixed, *((exclude,) if exclude is not None else ())), slots, canonical
        )
        need = self.need - self.hall[:, list(fixed)].sum(axis=1)
        extra = float(self.values[list(fixed)].sum())
        if slots == 0:
            if np.any(need > 0) or (
                target is not None and extra < target - self.parameters.value_tolerance
            ):
                return Infeasible(reason="fixed roster does not meet positions or target value")
            return self.plan(fixed, 0)
        if len(candidates) < slots:
            return Infeasible(reason="insufficient eligible market candidates")
        cost = np.array([self.costs[i] for i in candidates], dtype=float)
        value = self.values[candidates]
        matrix = np.vstack((np.ones(len(candidates)), cost, self.hall[:, candidates]))
        lower = np.r_[slots, 0, need]
        upper = np.r_[slots, self.budget - fixed_cost, np.full(len(need), np.inf)]
        if target is not None:
            matrix = np.vstack((matrix, value))
            lower = np.r_[lower, target - extra - self.parameters.value_tolerance]
            upper = np.r_[upper, np.inf]
        score = -value if objective == "value" else cost
        chosen = self.optimize(score, matrix, lower, upper)
        if chosen is None:
            return Infeasible(reason="no legal completion at the current costs and value target")
        if canonical:
            chosen = self.canonical_choice(score, matrix, lower, upper, chosen)
        picked = tuple(candidates[j] for j in chosen)
        result = self.plan((*picked, *fixed), sum(self.costs[i] or 0 for i in picked))
        if (
            len(result.purchases) != self.slots
            or len(result.assignments) != len(self.league.starter_slots)
            or result.cost + fixed_cost > self.budget
        ):
            raise SolverError("portfolio: optimal solution violates roster or budget constraints")
        return result

    def optimize(
        self,
        score: NDArray[np.float64],
        matrix: NDArray[np.float64],
        lower: NDArray[np.float64],
        upper: NDArray[np.float64],
    ) -> tuple[int, ...] | None:
        self.calls += 1
        result = milp(
            score,
            integrality=np.ones(len(score)),
            bounds=Bounds(0, 1),
            constraints=LinearConstraint(matrix, lower, upper),
            options={"time_limit": self.parameters.time_limit_seconds, "mip_rel_gap": 0.0},
        )
        if result.status == 2:
            return None
        if result.status == 1:
            raise CalculationTimeout("portfolio: configured solver deadline exceeded")
        if result.status != 0 or result.x is None:
            raise SolverError(f"portfolio: solver failed with status {result.status}")
        return tuple(i for i, x in enumerate(result.x) if x > 1 / 2)

    def canonical_choice(
        self,
        score: NDArray[np.float64],
        matrix: NDArray[np.float64],
        lower: NDArray[np.float64],
        upper: NDArray[np.float64],
        chosen: tuple[int, ...],
    ) -> tuple[int, ...]:
        optimum = fsum(float(score[i]) for i in chosen)
        tolerance = self.parameters.value_tolerance
        matrix = np.vstack((matrix, score))
        lower = np.r_[lower, optimum - tolerance]
        upper = np.r_[upper, optimum + tolerance]
        # First prove uniqueness. Most real-valued portfolios need no lexicographic passes.
        other = np.zeros(len(score))
        other[list(chosen)] = 1
        alternative = self.optimize(other, matrix, lower, upper)
        if alternative == chosen:
            return chosen
        selected: list[int] = []
        for i in range(len(score)):
            prefer = np.zeros(len(score))
            prefer[i] = -1
            best = self.optimize(prefer, matrix, lower, upper)
            if best is None:
                raise SolverError("portfolio: canonical optimum became infeasible")
            take = i in best
            if take:
                selected.append(i)
            row = -prefer
            matrix = np.vstack((matrix, row))
            lower = np.r_[lower, int(take)]
            upper = np.r_[upper, int(take)]
            if len(selected) == len(chosen):
                return tuple(selected)
        raise SolverError("portfolio: canonical optimum did not fill the roster")

    def plan(self, chosen: tuple[int, ...], cost: int) -> Plan:
        purchases = tuple(sorted(self.players[i].id for i in chosen))
        ids = tuple(sorted((*self.owned, *purchases)))
        return Plan(
            players=ids,
            purchases=purchases,
            assignments=assign(self.league, tuple(p for p in self.players if p.id in ids)),
            cost=cost,
            utility=fsum(float(self.values[i]) for i in chosen),
        )

    def cap(self, player: int, base: Plan) -> CapCalculation:
        without = self.solve(exclude=player) if self.players[player].id in base.purchases else base
        forced = isinstance(without, Infeasible)
        target = None if isinstance(without, Infeasible) else without.utility
        loss = None if isinstance(without, Infeasible) else max(0.0, base.utility - without.utility)
        if target is not None and self.prune and self.cannot_afford_value(player, target):
            return CapCalculation(0, loss, forced, ())
        result = self.solve(force=player, target=target, objective="cost")
        if isinstance(result, Infeasible):
            return CapCalculation(0, loss, forced, ())
        maximum = evaluate(
            "affordable_cap",
            budget=float(self.budget),
            slots=float(self.slots),
            minimum=float(self.league.minimum_bid),
            completion_cost=float(result.cost),
            increment=float(self.league.bid_increment),
        )
        return CapCalculation(int(maximum.result), loss, forced, (maximum,))

    def cannot_afford_value(self, player: int, target: float) -> bool:
        slots = self.slots - 1
        available = np.flatnonzero(self.available)
        if slots <= 0 or len(available) < self.slots:
            return False
        spread = float(np.ptp(self.values[available]))
        if spread <= self.parameters.value_tolerance:
            return False
        costs = np.array([self.costs[i] for i in available], dtype=float)
        scale = max(1.0, float(np.ptp(costs))) / spread
        multipliers = np.array(self.parameters.bound_multipliers) * scale
        scores = np.nextafter(
            np.nextafter(multipliers[:, None] * self.values[available], np.inf) - costs, np.inf
        )
        scores[:, available == player] = -np.inf
        top = np.sort(scores, axis=1)[:, ::-1][:, :slots]
        total = np.zeros(len(multipliers))
        for column in range(slots):
            total = np.nextafter(total + top[:, column], np.inf)
        required = np.nextafter(
            np.nextafter(target - self.values[player], -np.inf) - self.parameters.value_tolerance,
            -np.inf,
        )
        lower = np.nextafter(np.nextafter(multipliers * required, -np.inf) - total, -np.inf)
        return bool(
            np.isfinite(lower).any()
            and np.max(lower) > self.budget - self.league.minimum_bid + 1e-5 * max(1, self.budget)
        )
