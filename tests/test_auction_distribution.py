import json
from math import floor, fsum
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize import Bounds, LinearConstraint, milp
from test_auction import independent_legal, inputs_for, state
from test_market_distribution import bid_mass

from fba.auction.auction import calculate_auction
from fba.contracts.auction import AuctionPlayer, Plan
from fba.formulas.market import normalization_shift


def opening_price_oracle(league, parameters, anchor):
    minimum, increment = league.minimum_bid, league.bid_increment
    maximum = league.budget - (len(league.starter_slots) + league.bench_slots - 1) * minimum
    shift = normalization_shift(league.teams, parameters.volatility)
    pmf = bid_mass(
        maximum, max(0, anchor - minimum), parameters.volatility, shift, minimum, increment
    )
    # Identical opening bidders: binomial order statistics from one scalar PMF.
    previous, cumulative = 0.0, 0.0
    sale_tails, high_tails = [], []
    for _, mass in pmf[:-1]:
        cumulative += mass
        sale_tails.append(
            1
            - cumulative**league.teams
            - league.teams * (1 - cumulative) * previous ** (league.teams - 1)
        )
        high_tails.append(1 - cumulative ** (league.teams - 1))
        previous = cumulative
    expected = minimum + increment * fsum(sale_tails)
    acquisition = minimum + increment * (fsum(high_tails) + (high_tails[0] if high_tails else 0))
    cost = max(minimum, floor(acquisition / increment + 0.5) * increment)
    return expected, acquisition, cost


class AssignmentOracle:
    """Independent player/slot assignment MILP, without production Hall constraints or pruning."""

    def __init__(self, league, players, costs):
        self.league, self.players, self.costs = league, players, costs
        self.n = len(players)
        pairs = [
            (i, s)
            for i, p in enumerate(players)
            for s, slot in enumerate(league.starter_slots)
            if set(p.positions) & set(slot.eligible_positions)
        ]
        size = self.n + len(pairs)
        rows, lower, upper = [], [], []
        row = np.zeros(size)
        row[: self.n] = 1
        rows.append(row)
        count = len(league.starter_slots) + league.bench_slots
        lower.append(count)
        upper.append(count)
        for s in range(len(league.starter_slots)):
            rows.append(np.array([0] * self.n + [int(slot == s) for _, slot in pairs]))
            lower.append(1)
            upper.append(1)
        for i in range(self.n):
            row = np.zeros(size)
            row[i] = -1
            for j, (p, _) in enumerate(pairs):
                if i == p:
                    row[self.n + j] = 1
            rows.append(row)
            lower.append(-np.inf)
            upper.append(0)
        self.rows, self.lower, self.upper = rows, lower, upper
        self.values = np.array([p.utility or 0 for p in players] + [0] * len(pairs))
        self.price = np.array([c or 0 for c in costs] + [0] * len(pairs), dtype=float)
        self.allowed = np.array(
            [
                float(c is not None and p.active and p.utility is not None)
                for c, p in zip(costs, players, strict=True)
            ]
            + [1] * len(pairs)
        )

    def solve(self, *, exclude=None, force=None, target=None):
        lo, hi = np.zeros(len(self.values)), self.allowed.copy()
        cost = self.price.copy()
        if exclude is not None:
            hi[exclude] = 0
        if force is not None:
            lo[force] = hi[force] = 1
            cost[force] = 0
        budget = self.league.budget - (self.league.minimum_bid if force is not None else 0)
        rows, lower, upper = [*self.rows, cost], [*self.lower, -np.inf], [*self.upper, budget]
        if target is not None:
            rows.append(self.values)
            lower.append(target - 1e-7)
            upper.append(np.inf)
        solved = milp(
            cost if target is not None else -self.values,
            integrality=np.ones(len(self.values)),
            bounds=Bounds(lo, hi),
            constraints=LinearConstraint(np.array(rows), lower, upper),
            options={"mip_rel_gap": 0.0, "time_limit": 10.0},
        )
        assert solved.status in (0, 2), solved.message
        return None if solved.status == 2 else np.rint(solved.x)


def test_frozen_opening_distribution_and_caps_match_independent_assignment_oracle():
    reference = json.loads((Path(__file__).parent / "fixtures/auction-reference.json").read_text())
    players = tuple(AuctionPlayer.model_validate_json(json.dumps(p)) for p in reference["players"])
    players = tuple(sorted(players, key=lambda p: p.id))
    inputs = inputs_for(players)
    result = calculate_auction(inputs, state(inputs), "0" * 64, "3" * 64)
    old = {p["id"]: p for p in reference["expected"]}
    costs = []
    for quote in result.market.prices:
        # The historical migration answers remain frozen. Method v2 deliberately
        # replaces sampled prices; validate expectations independently, not against one seed.
        assert quote.anchor == pytest.approx(old[quote.player_id]["anchor"], abs=0.01)
        if quote.anchor is None:
            assert quote.expected is quote.acquisition is quote.planning_cost is None
            costs.append(None)
        else:
            expected, acquisition, cost = opening_price_oracle(
                inputs.config.league, inputs.config.model.market, quote.anchor
            )
            assert quote.expected == pytest.approx(expected, abs=1e-9)
            assert quote.acquisition == pytest.approx(acquisition, abs=1e-9)
            assert quote.planning_cost == cost
            costs.append(cost)
    assert isinstance(result.plan, Plan)
    held = tuple(p for p in players if p.id in result.plan.players)
    assert independent_legal(inputs.config.league, held)
    oracle = AssignmentOracle(inputs.config.league, players, costs)
    best = oracle.solve()
    assert best is not None
    value = float(best @ oracle.values)
    assert result.plan.utility == pytest.approx(value, abs=1e-6)
    for i, cap in enumerate(result.caps):
        if not players[i].active or players[i].utility is None:
            assert cap.amount is None
            continue
        skipped = oracle.solve(exclude=i) if best[i] else best
        assert skipped is not None
        bought = oracle.solve(force=i, target=float(skipped @ oracle.values))
        expected = (
            0
            if bought is None
            else inputs.config.league.budget
            - int(bought @ oracle.price - bought[i] * oracle.price[i])
        )
        assert cap.amount == expected, cap.player_id
