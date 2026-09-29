import numpy as np
import pytest
from test_auction import config, inputs_for, player, state

from fba.contracts.auction import DraftOverride, Plan, Sale
from fba.core.auction import calculate_auction, market_context
from fba.core.market import opening_anchors


def small_league(teams=2):
    league = config().league
    return league.model_copy(
        update={
            "teams": teams,
            "budget": 50,
            "starter_slots": league.starter_slots[:1],
            "bench_slots": 1,
        }
    )


def test_cutoff_ties_share_anchor_and_sparse_quotes_obey_individual_budget():
    league = small_league()
    rows = tuple(player(i, quote=q) for i, q in enumerate((50.0, 10.0, 10.0, 10.0, 10.0, 1.0)))
    anchors = opening_anchors(league, rows)
    assert len({anchors[p.id] for p in rows[1:5]}) == 1
    assert anchors[rows[0].id] == 49
    assert anchors[rows[1].id] == pytest.approx(17)
    assert max(anchors.values()) <= 49
    assert sum(sorted(anchors.values(), reverse=True)[:4]) == pytest.approx(100)
    sparse = opening_anchors(league, (player(0, quote=50.0), player(1, quote=None)))
    assert sparse == {"000": 49.0, "001": None}
    assert opening_anchors(league, rows[::-1]) == anchors


@pytest.mark.parametrize("seed", range(10))
def test_anchor_normalization_matches_independent_bounded_cash_equation(seed):
    league = small_league()
    rng = np.random.default_rng(seed)
    quotes = tuple(float(x) for x in rng.choice((0, 0.001, 1, 7, 50, 10000), size=7))
    rows = tuple(player(i, quote=q) for i, q in enumerate(quotes))
    actual = opening_anchors(league, rows)
    top = sorted(quotes, reverse=True)[:4]
    target = min(100, sum(49 if q else 1 for q in top))
    low, high = 0.0, max((49 / q for q in top if q), default=0.0)
    for _ in range(100):
        mid = (low + high) / 2
        if sum(min(49, max(1, q * mid)) for q in top) < target:
            low = mid
        else:
            high = mid
    for row in rows:
        expected = (
            min(49, max(1, row.projected_price * high)) if row.projected_price >= top[-1] else 1
        )
        assert actual[row.id] == pytest.approx(expected, abs=1e-8)


@pytest.mark.parametrize("quote", [1.0, 1.000001, 1.01])
def test_near_floor_endgame_has_a_real_affordable_plan(quote):
    league = small_league()
    inputs = inputs_for(tuple(player(i, quote=0.0) for i in range(8)), league)
    draft = state(
        inputs,
        (
            Sale(id="mine", player_id="000", buyer="team-00", amount=49),
            Sale(id="foe", player_id="001", buyer="team-01", amount=1),
        ),
    )
    draft = draft.model_copy(
        update={
            "overrides": tuple(
                DraftOverride(player_id=p.id, market=quote, positions=None, reason="Boundary case")
                for p in inputs.players[2:]
            )
        }
    )
    result = calculate_auction(inputs, draft, "0" * 64, "1" * 64)
    assert isinstance(result.plan, Plan)
    assert result.plan.cost == 1
    assert all(q.planning_cost == 1 for q in result.market.prices[2:])
    assert all(c.amount == 1 for c in result.caps[2:])
    assert result.market.inflation <= 25


def test_more_own_cash_does_not_change_the_rule_for_beating_the_same_foe_bid():
    league = small_league()
    inputs = inputs_for(tuple(player(i, quote=0.0) for i in range(8)), league)
    parameters = inputs.config.model.market.model_copy(
        update={"volatility": 0.0, "wealth_exponent": 0.0}
    )
    inputs = inputs.model_copy(
        update={
            "config": inputs.config.model_copy(
                update={"model": inputs.config.model.model_copy(update={"market": parameters})}
            )
        }
    )
    costs = []
    for cash in range(1, 50):
        draft = state(
            inputs,
            (
                Sale(id="mine", player_id="000", buyer="team-00", amount=50 - cash),
                Sale(id="foe", player_id="001", buyer="team-01", amount=1),
            ),
        )
        draft = draft.model_copy(
            update={
                "overrides": tuple(
                    DraftOverride(
                        player_id=p.id, market=3.0, positions=None, reason="Boundary case"
                    )
                    for p in inputs.players[2:]
                )
            }
        )
        _, market = market_context(inputs, draft, "0" * 64)
        costs.append(market.prices[2].planning_cost)
    assert costs == sorted(costs)


def test_market_distributions_do_not_depend_on_team_identifiers():
    league = small_league(3)
    inputs = inputs_for(
        tuple(player(i, quote=float(i + 10), utility=float(i)) for i in range(12)), league
    )
    draft = state(
        inputs,
        tuple(
            Sale(id=str(i), player_id=f"{i:03}", buyer=f"team-{i:02}", amount=amount)
            for i, amount in enumerate((5, 15, 25))
        ),
    )
    mapping = {"team-00": "z", "team-01": "b", "team-02": "a"}
    renamed = draft.model_copy(
        update={
            "mine": mapping[draft.mine],
            "teams": tuple(t.model_copy(update={"id": mapping[t.id]}) for t in draft.teams),
            "sales": tuple(s.model_copy(update={"buyer": mapping[s.buyer]}) for s in draft.sales),
        }
    )
    _, before = market_context(inputs, draft, "0" * 64)
    _, after = market_context(inputs, renamed, "0" * 64)
    assert before.prices == after.prices
    assert before.inflation == after.inflation
