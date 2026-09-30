from test_auction import inputs_for, player, state
from test_market import small_league

from fba.auction.auction import calculate_auction
from fba.contracts.auction import Plan, Sale


def room():
    inputs = inputs_for(
        tuple(player(i, quote=float(i + 5), utility=float(20 - i)) for i in range(12)),
        small_league(3),
    )
    draft = state(
        inputs,
        (
            Sale(id="a", player_id="000", buyer="team-00", amount=1),
            Sale(id="b", player_id="001", buyer="team-01", amount=1),
            Sale(id="c", player_id="002", buyer="team-02", amount=24),
            Sale(id="d", player_id="003", buyer="team-02", amount=25),
        ),
    )
    return inputs, draft


def test_nomination_requires_two_opponents_and_an_affordable_plan_member():
    inputs, draft = room()
    result = calculate_auction(inputs, draft, "0" * 64, "1" * 64)
    assert isinstance(result.plan, Plan)
    assert all(p.bidders <= 1 for p in result.market.prices)
    assert not result.nominations.drain
    assert result.nominations.target
    caps = {c.player_id: c for c in result.caps}
    prices = {p.player_id: p for p in result.market.prices}
    for pid in result.nominations.target:
        assert pid in result.plan.purchases
        assert caps[pid].amount >= prices[pid].planning_cost
        assert not caps[pid].conditional


def test_finished_roster_unused_cash_cannot_change_market_or_caps():
    inputs, draft = room()
    before = calculate_auction(inputs, draft, "0" * 64, "1" * 64)
    changed = draft.model_copy(
        update={
            "sales": tuple(
                s.model_copy(update={"amount": 1}) if s.id == "c" else s for s in draft.sales
            )
        }
    )
    after = calculate_auction(inputs, changed, "0" * 64, "2" * 64)
    assert sum(t.budget for t in before.market.room) != sum(t.budget for t in after.market.room)
    assert before.market.inflation == after.market.inflation
    assert before.market.prices == after.market.prices
    assert before.caps == after.caps
    assert before.plan == after.plan


def test_market_expectations_do_not_follow_own_roster_objective():
    inputs, draft = room()
    before = calculate_auction(inputs, draft, "0" * 64, "1" * 64)
    changed = inputs.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"utility": -p.utility, "fair": 0.0}) for p in inputs.players
            )
        }
    )
    after = calculate_auction(
        changed, draft.model_copy(update={"mine": "team-01"}), "0" * 64, "2" * 64
    )
    assert [(p.anchor, p.expected) for p in before.market.prices] == [
        (p.anchor, p.expected) for p in after.market.prices
    ]
