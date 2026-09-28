import itertools
import json
from pathlib import Path

import numpy as np
import pytest

from fba.adapters.codec import canonical
from fba.adapters.config import load_config
from fba.contracts.auction import (
    AuctionInput,
    AuctionPlayer,
    DraftState,
    DraftTeam,
    Infeasible,
    Plan,
    Sale,
    TeamBudget,
)
from fba.contracts.base import DataError
from fba.core.auction import calculate_auction, compare, market_context, portfolio_for
from fba.core.market import bidders_by_position, opening_anchors
from fba.core.portfolio import Portfolio
from fba.core.roster import assign, completable


def config():
    base = Path(__file__).parents[1] / "examples/2026-27"
    return load_config(*(base / f"{n}.json" for n in ("league", "season", "model")))


def player(i, positions=("PG",), utility=1.0, quote=1.0):
    return AuctionPlayer(
        id=f"{i:03}",
        name=str(i),
        positions=positions,
        positions_confirmed=True,
        active=True,
        projected_price=quote,
        fair=max(0.0, utility) if utility is not None else None,
        utility=utility,
    )


def state(inputs, sales=()):
    return DraftState(
        format_version=1,
        draft_id="test",
        revision=len(sales),
        config=inputs.config.refs,
        input_sha256="0" * 64,
        mine="team-00",
        teams=tuple(
            DraftTeam(id=f"team-{i:02}", name=str(i)) for i in range(inputs.config.league.teams)
        ),
        sales=sales,
        overrides=(),
    )


def inputs_for(players, league=None):
    c = config()
    if league is not None:
        c = c.model_copy(update={"league": league})
    return AuctionInput(
        format_version=1,
        config=c,
        artifacts=(),
        snapshot_sha256="1" * 64,
        calculation_sha256="2" * 64,
        players=players,
        management=None,
    )


def independent_legal(league, players):
    # Independent exhaustive slot-to-player assignments, not Hall or production matching.
    def fill(slots, remaining):
        if not slots:
            return True
        return any(
            set(p.positions) & set(slots[0].eligible_positions)
            and fill(slots[1:], remaining[:i] + remaining[i + 1 :])
            for i, p in enumerate(remaining)
        )

    return fill(league.starter_slots, players)


@pytest.mark.parametrize("bench", range(3))
def test_position_grouped_bidders_match_exhaustive_completion(bench):
    c = config()
    league = c.league.model_copy(
        update={"teams": 2, "starter_slots": c.league.starter_slots[:2], "bench_slots": bench}
    )
    shapes = (("PG",), ("SG",), ("PG", "SG"), ("SG", "PG"), ("C",))
    players = tuple(player(i, positions) for i, positions in enumerate(shapes * 2))
    size = len(league.starter_slots) + bench
    for count in range(size + 1):
        held = players[:count]
        room = tuple(
            TeamBudget(
                id=str(j),
                owned=tuple(p.id for p in held),
                budget=league.budget,
                slots=size - count,
                maximum_bid=league.budget,
            )
            for j in range(league.teams)
        )
        grouped = bidders_by_position(league, c.model.market, players, room, 1.0)
        fillers = tuple(player(100 + i, ("PG", "SG")) for i in range(size - count - 1))
        for candidate in players:
            possible = count < size and independent_legal(league, (*held, candidate, *fillers))
            assert bool(grouped[tuple(sorted(candidate.positions))]) == possible
        assert grouped == bidders_by_position(league, c.model.market, players[::-1], room, 1.0)


@pytest.mark.parametrize("seed", range(12))
def test_integer_caps_and_pruning_match_independent_exhaustive_oracle(seed):
    c = config()
    league = c.league.model_copy(
        update={"starter_slots": c.league.starter_slots[:2], "bench_slots": 1}
    )
    rng = np.random.default_rng(seed)
    positions = [("PG",), ("SG",), ("PG", "SG")]
    players = tuple(
        player(i, positions[int(rng.integers(3))], float(rng.integers(-3, 8))) for i in range(9)
    )
    costs = tuple(int(x) for x in rng.integers(1, 6, len(players)))
    budget = 9
    subsets = [
        s
        for s in itertools.combinations(range(len(players)), 3)
        if independent_legal(league, tuple(players[i] for i in s))
    ]
    affordable = [s for s in subsets if sum(costs[i] for i in s) <= budget]
    if not affordable:
        pytest.skip("synthetic pool lacks feasible completion")
    best = min(affordable, key=lambda s: (-sum(players[i].utility for i in s), s))
    expected = tuple(players[i].id for i in best)
    for prune in (False, True):
        p = Portfolio(
            league, c.model.solver, players, costs, (True,) * len(players), (), budget, prune=prune
        )
        base = p.solve(canonical=True)
        assert isinstance(base, Plan)
        assert base.purchases == expected
        for i in range(len(players)):
            skipped = [s for s in affordable if i not in s]
            target = max((sum(players[j].utility for j in s) for s in skipped), default=-np.inf)
            cap = max(
                (
                    price
                    for price in range(1, budget - 3 + 2)
                    if any(
                        i in s
                        and sum(costs[j] for j in s if j != i) + price <= budget
                        and sum(players[j].utility for j in s) >= target - 1e-7
                        for s in subsets
                    )
                ),
                default=0,
            )
            assert p.cap(i, base)[0] == cap


@pytest.mark.parametrize("minimum", [1, 2])
def test_endgame_every_integer_budget_and_null_quotes(minimum):
    c = config()
    league = c.league.model_copy(
        update={
            "teams": 2,
            "budget": 15 * minimum,
            "minimum_bid": minimum,
            "starter_slots": c.league.starter_slots[:1],
            "bench_slots": 4,
        }
    )
    players = tuple(player(i, quote=0.0) for i in range(12)) + (player(12, quote=None),)
    inp = inputs_for(players, league)
    for remaining in range(5 * minimum, 15 * minimum + 1):
        # Direct portfolio tests isolate the low-cash integer completeness contract.
        p = Portfolio(
            league,
            c.model.solver,
            players,
            (minimum,) * 12 + (None,),
            (True,) * 12 + (False,),
            (),
            remaining,
        )
        base = p.solve(canonical=True)
        assert isinstance(base, Plan)
        assert base.cost == 5 * minimum
        assert p.cap(0, base)[0] == remaining - 4 * minimum
    result = calculate_auction(inp, state(inp), "0" * 64, "3" * 64)
    missing = next(q for q in result.market.prices if q.player_id == players[-1].id)
    assert (
        missing.anchor is missing.expected is missing.acquisition is missing.planning_cost is None
    )
    assert players[-1].id not in result.plan.purchases


def test_original_additive_opening_prices_plan_and_caps():
    reference = json.loads((Path(__file__).parent / "fixtures/auction-reference.json").read_text())
    players = tuple(AuctionPlayer.model_validate_json(json.dumps(p)) for p in reference["players"])
    inp = inputs_for(players)
    result = calculate_auction(inp, state(inp), "0" * 64, "3" * 64)
    by = {p["id"]: p for p in reference["expected"]}
    for p in result.market.prices:
        expected = by[p.player_id]
        for field in ("anchor", "expected", "acquisition", "planning_cost"):
            actual = getattr(p, field)
            key = "planned_cost" if field == "planning_cost" else field
            assert (
                actual == pytest.approx(expected[key], abs=0.01)
                if actual is not None
                else expected[key] is None
            )
    assert sorted(result.plan.purchases) == sorted(reference["plan"])
    assert result.plan.cost == reference["plan_cost"]
    assert {c.player_id: c.amount for c in result.caps} == {
        p["id"]: p["stop"] for p in reference["expected"]
    }


def test_shuffle_state_validation_and_buy_skip():
    c = config()
    league = c.league.model_copy(
        update={"teams": 2, "starter_slots": c.league.starter_slots[:1], "bench_slots": 1}
    )
    inp = inputs_for(tuple(player(i, utility=float(i)) for i in range(6)), league)
    s = state(inp)
    original = calculate_auction(inp, s, "0" * 64, "3" * 64)
    shuffled = calculate_auction(
        inp.model_copy(update={"players": tuple(reversed(inp.players))}),
        s.model_copy(update={"teams": tuple(reversed(s.teams))}),
        "0" * 64,
        "3" * 64,
    )
    assert canonical(original) == canonical(shuffled)
    players, market = market_context(inp, s, "0" * 64)
    p = portfolio_for(inp, players, market, s)
    comparison = compare(p, original.plan.purchases[0], 1)
    assert isinstance(comparison.buy, Plan) and isinstance(comparison.skip, Plan)
    assert len(comparison.buy.players) == len(comparison.skip.players) == 2
    sale = Sale(id="one", player_id=players[0].id, buyer=s.mine, amount=200)
    with pytest.raises(DataError, match="amount"):
        market_context(inp, state(inp, (sale,)), "0" * 64)
    with pytest.raises(DataError, match="hash"):
        market_context(inp, s, "4" * 64)


def test_position_matching_and_no_plan_are_explicit():
    c = config()
    league = c.league.model_copy(
        update={"starter_slots": c.league.starter_slots[:2], "bench_slots": 0}
    )
    players = (player(0), player(1, ("SG",)))
    assert independent_legal(league, players)
    assert len(assign(league, players)) == 2
    assert not completable(league, (players[0], player(2)))
    p = Portfolio(league, c.model.solver, players, (2, 2), (True, True), (), 3)
    assert isinstance(p.solve(), Infeasible)
    assert opening_anchors(league, (player(0, quote=None),)) == {"000": None}


@pytest.mark.parametrize("teams", [12, 16])
def test_configured_league_variants_keep_legal_plans(teams):
    from fba.contracts.config import Category, CategoryFloor, Linear, StarterSlot, Term
    from fba.core.config import validate_config

    original = config()
    categories = tuple(c for c in original.league.categories if c.id not in ("OREB", "DD", "A/T"))
    categories += (
        Category(
            id="TO",
            label="Turnovers",
            formula=Linear(kind="linear", terms=(Term(stat_id="TO", coefficient=1.0),)),
            direction="lower",
            comparison_decimals=6,
            tie_value=0.5,
        ),
    )
    league = original.league.model_copy(
        update={
            "teams": teams,
            "categories": categories,
            "starter_slots": (
                *original.league.starter_slots,
                StarterSlot(id="util", label="UTIL", eligible_positions=original.league.positions),
            ),
            "bench_slots": 3,
            "lineup": original.league.lineup.model_copy(
                update={"lock_mode": "weekly", "lock_at": "period_start"}
            ),
            "transactions": original.league.transactions.model_copy(update={"adds_per_period": 3}),
        }
    )
    floors = tuple(
        c for c in original.model.fit.category_floors if c.id in {c.id for c in categories}
    ) + (CategoryFloor(id="TO", value=1.0),)
    model = original.model.model_copy(
        update={
            "fit": original.model.fit.model_copy(update={"category_floors": floors}),
            "market": original.model.market.model_copy(
                update={"samples": 16, "normalization_samples": 32}
            ),
        }
    )
    changed = validate_config(league, original.season, model, original.refs)
    players = tuple(
        player(i, league.positions, utility=float(i), quote=0.0) for i in range(teams * 12 + 2)
    )
    inp = inputs_for(players).model_copy(update={"config": changed})
    result = calculate_auction(inp, state(inp), "0" * 64, "3" * 64)
    assert isinstance(result.plan, Plan)
    assert len(result.plan.players) == 12 and len(result.plan.assignments) == 9
    assert result.plan.cost <= league.budget
    assert all(p.player_id in result.plan.players for p in result.plan.assignments)


def test_solver_failure_and_timeout_never_publish_an_approximate_solution(monkeypatch):
    from types import SimpleNamespace

    from fba.contracts.auction import CalculationTimeout, SolverError
    from fba.core import portfolio

    c = config()
    league = c.league.model_copy(
        update={"starter_slots": c.league.starter_slots[:1], "bench_slots": 0}
    )
    p = Portfolio(league, c.model.solver, (player(0), player(1)), (1, 1), (True, True), (), 5)
    for status, error in ((1, CalculationTimeout), (4, SolverError)):
        monkeypatch.setattr(
            portfolio,
            "milp",
            lambda *args, _status=status, **kwargs: SimpleNamespace(status=_status, x=None),
        )
        with pytest.raises(error):
            p.solve()
    monkeypatch.setattr(
        portfolio, "milp", lambda *args, **kwargs: SimpleNamespace(status=2, x=None)
    )
    assert isinstance(p.solve(), Infeasible)


def test_parallel_caps_are_identical_on_a_changed_state():
    from fba.apps.auction import AuctionSession

    c = config()
    league = c.league.model_copy(
        update={"teams": 2, "starter_slots": c.league.starter_slots[:1], "bench_slots": 1}
    )
    inputs = inputs_for(tuple(player(i, utility=float(i), quote=0.0) for i in range(8)), league)
    sale = Sale(id="sale", player_id="000", buyer="team-01", amount=5)
    draft = state(inputs, (sale,))
    session = AuctionSession(2)
    try:
        fast = calculate_auction(inputs, draft, "0" * 64, "3" * 64, runner=session.caps)
        slow = calculate_auction(inputs, draft, "0" * 64, "3" * 64)
        assert canonical(fast) == canonical(slow)
        players, market = market_context(inputs, draft, "0" * 64)
        for candidate, price in (("001", 1), ("004", 5), ("007", 100)):
            slow_comparison = compare(
                portfolio_for(inputs, players, market, draft), candidate, price
            )
            fast_comparison = compare(
                portfolio_for(inputs, players, market, draft), candidate, price, session.comparison
            )
            assert canonical(fast_comparison) == canonical(slow_comparison)
    finally:
        session.close()


@pytest.mark.parametrize("remaining", range(5, 16))
def test_endgame_market_nomination_cost_has_no_budget_holes(remaining):
    c = config()
    league = c.league.model_copy(
        update={"teams": 2, "starter_slots": c.league.starter_slots[:1], "bench_slots": 9}
    )
    inputs = inputs_for(tuple(player(i, quote=0.0) for i in range(24)), league)
    sales = tuple(
        Sale(
            id=f"sale-{i}",
            player_id=f"{i:03}",
            buyer=f"team-{i // 5:02}",
            amount=league.budget - (remaining if i < 5 else 5) - 4 if i % 5 == 0 else 1,
        )
        for i in range(10)
    )
    result = calculate_auction(inputs, state(inputs, sales), "0" * 64, "3" * 64)
    assert isinstance(result.plan, Plan) and result.plan.cost == 5
    assert all(q.planning_cost == 1 for q in result.market.prices[10:])
    assert all(c.amount == remaining - 4 for c in result.caps[10:])


def test_complete_roster_conditional_caps_and_invalid_counterfactual():
    c = config()
    league = c.league.model_copy(
        update={"teams": 2, "starter_slots": c.league.starter_slots[:1], "bench_slots": 0}
    )
    players = (player(0), player(1, utility=None), player(2, ("SG",)), player(3, quote=None))
    inp = inputs_for(players, league)
    result = calculate_auction(inp, state(inp), "0" * 64, "3" * 64)
    assert result.caps[1].amount is None and result.caps[2].amount == 0
    assert result.caps[3].conditional
    sale = Sale(id="sold", player_id="000", buyer="team-00", amount=1)
    draft = state(inp, (sale,))
    full = calculate_auction(inp, draft, "0" * 64, "3" * 64)
    assert full.plan.purchases == () and full.caps[3].amount == 0
    catalog, market = market_context(inp, state(inp), "0" * 64)
    p = portfolio_for(inp, catalog, market, state(inp))
    with pytest.raises(DataError, match="unknown"):
        compare(p, "unknown", 1)
    with pytest.raises(DataError, match="unavailable"):
        compare(p, "003", 1)
    with pytest.raises(DataError, match="price"):
        compare(p, "000", league.budget + 1)


def test_solver_rejects_impossible_forcing_and_invalid_optimal_responses(monkeypatch):
    from fba.contracts.auction import SolverError

    c = config()
    league = c.league.model_copy(
        update={"starter_slots": c.league.starter_slots[:1], "bench_slots": 0}
    )
    p = Portfolio(
        league, c.model.solver, (player(0), player(1, ("SG",))), (1, 1), (True, True), (), 0
    )
    assert isinstance(p.solve(force=0), Infeasible)
    p.budget = 2
    assert isinstance(p.solve(force=1), Infeasible)
    assert isinstance(p.solve(force=0, target=2.0), Infeasible)
    assert isinstance(p.solve(force=0), Plan)
    original = p.plan
    monkeypatch.setattr(
        p,
        "plan",
        lambda picked, cost: original(picked, cost).model_copy(update={"assignments": ()}),
    )
    with pytest.raises(SolverError, match="violates"):
        p.solve()
    matrix = np.ones((1, 2))
    lower = np.ones(1)
    upper = np.ones(1)
    calls = iter(((), None))
    monkeypatch.setattr(p, "optimize", lambda *args: next(calls))
    with pytest.raises(SolverError, match="infeasible"):
        p.canonical_choice(np.zeros(2), matrix, lower, upper, (0,))
    monkeypatch.setattr(p, "optimize", lambda *args: ())
    with pytest.raises(SolverError, match="fill"):
        p.canonical_choice(np.zeros(2), matrix, lower, upper, (0,))


def test_counterfactual_cannot_fill_missing_position_or_use_wrong_model():
    from fba.contracts.base import ConfigError

    c = config()
    league = c.league.model_copy(
        update={"teams": 2, "starter_slots": c.league.starter_slots[:2], "bench_slots": 0}
    )
    players = (
        player(0, quote=0.0),
        player(1, ("SG",), quote=0.0),
        player(2, quote=0.0),
        player(3, ("SG",), quote=0.0),
    )
    inp = inputs_for(players, league)
    draft = state(
        inp,
        (
            Sale(id="a", player_id="000", buyer="team-00", amount=1),
            Sale(id="b", player_id="001", buyer="team-01", amount=1),
        ),
    )
    catalog, market = market_context(inp, draft, "0" * 64)
    comparison = compare(portfolio_for(inp, catalog, market, draft), "002", 1)
    assert isinstance(comparison.buy, Infeasible) and comparison.delta is None
    broken = inp.model_copy(update={"config": inp.config.model_copy(update={"model": league})})
    with pytest.raises(ConfigError, match="model"):
        market_context(broken, draft, "0" * 64)
