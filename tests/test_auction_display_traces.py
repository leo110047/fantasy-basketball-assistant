import pytest
from test_auction import config, inputs_for, player, state
from test_market import small_league

from fba.auction.auction import compare, market_context, portfolio_for
from fba.contracts.auction import MarketPrice, Plan, TeamBudget
from fba.formulas.market import quoted_gap, room_summary
from fba.formulas.registry import evaluate


@pytest.mark.parametrize(
    "fair, expected, difference, discount, focused",
    (
        (25, 20, 5, 0.2, True),
        (100, 95, 5, 0.05, False),
        (20, 16, 4, 0.2, False),
        (0, 2, -2, None, False),
        (None, 2, None, None, False),
        (25, None, None, None, False),
    ),
)
def test_market_gap_has_independent_answers_and_replayable_trace(
    fair, expected, difference, discount, focused
):
    quote = MarketPrice(
        player_id="000",
        anchor=2,
        expected=expected,
        acquisition=None,
        planning_cost=None,
        bidders=0,
    )
    result = quoted_gap(player(0).model_copy(update={"fair": fair}), quote, config().model.market)
    assert (result.difference, result.discount, result.focused) == (difference, discount, focused)
    for trace in result.traces:
        assert evaluate(trace.formula_id, **trace.inputs) == trace


def test_focus_thresholds_are_configurable_without_changing_prices_or_cap():
    quote = MarketPrice(
        player_id="000", anchor=2, expected=16, acquisition=17, planning_cost=17, bidders=2
    )
    params = config().model.market.model_copy(update={"focus_difference": 4, "focus_discount": 0.2})
    result = quoted_gap(player(0).model_copy(update={"fair": 20}), quote, params)
    assert result.focused
    assert (result.expected, result.acquisition, result.planning_cost) == (16, 17, 17)


def test_market_room_summary_separates_full_teams_and_replays_exact_inputs():
    room = (
        TeamBudget(id="a", owned=(), budget=10, slots=0, maximum_bid=0),
        TeamBudget(id="b", owned=(), budget=17, slots=2, maximum_bid=16),
    )
    summary = room_summary(room)
    assert (summary.cash, summary.spendable, summary.slots) == (27, 17, 2)
    assert tuple(trace.result for trace in summary.traces) == (27, 17, 2)
    assert summary.traces[0].inputs["values"] == (10, 17)
    assert summary.traces[1].inputs["values"] == (17,)
    for trace in summary.traces:
        assert evaluate(trace.formula_id, **trace.inputs) == trace


def test_cached_market_quote_keeps_individual_fair_gap_and_comparison_cash():
    rows = tuple(player(i, ("PG", "SG", "SF", "PF", "C"), utility=i + 1, quote=0) for i in range(8))
    inputs = inputs_for(rows, small_league())
    draft = state(inputs)
    catalog, market = market_context(inputs, draft, "0" * 64)
    a, b = market.prices[:2]
    assert a.expected == b.expected
    assert b.difference - a.difference == pytest.approx(1)
    assert a.traces[-2].inputs["after"] == 1
    assert b.traces[-2].inputs["after"] == 2
    result = compare(portfolio_for(inputs, catalog, market, draft), "007", 3)
    assert isinstance(result.buy, Plan) and isinstance(result.skip, Plan)
    assert result.remaining_budget == {"buy": 50 - result.buy.cost, "skip": 50 - result.skip.cost}
    assert result.traces[0].result == pytest.approx(result.delta, abs=5e-7)
    for trace in result.traces:
        assert evaluate(trace.formula_id, **trace.inputs) == trace
