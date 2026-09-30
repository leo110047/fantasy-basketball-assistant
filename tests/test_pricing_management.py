import json

import numpy as np
import pytest
from test_fit import fitted_case as fitted_case
from test_managed import reference_manager
from test_season import management_parameters

from fba.adapters.native import NativeKernel
from fba.apps.auction import AuctionSession
from fba.auction.auction import calculate_auction
from fba.contracts.config import ManagedPricingModel, PricingParameters
from fba.data.codec import canonical


class RecordedKernel:
    def __init__(self, native):
        self.native = native
        self.calls = []

    def __call__(self, *args):
        raise AssertionError("Complete pricing must use the shared tactical core")

    def run(self, arrays, rosters, pool, tactics, trace, *, primary_only=False):
        assert tactics is not None
        result = self.native.run(arrays, rosters, pool, tactics, trace, primary_only=primary_only)
        self.calls.append((arrays.add_limit, rosters, tactics.policy, result.adds.copy()))
        return result


@pytest.mark.parametrize("limit", [0, 3, 4, 6])
def test_configured_policy_is_used_by_pricing_projection(limit):
    native = NativeKernel()
    try:
        recorded = RecordedKernel(native)
        manager, spec, _ = reference_manager(recorded, 1)
        manager.league = manager.league.model_copy(
            update={
                "transactions": manager.league.transactions.model_copy(
                    update={"adds_per_period": limit}
                )
            }
        )
        manager.pricing = PricingParameters(
            streaming_slots=2, upgrades=True, evidence=manager.parameters.evidence
        )
        manager.tactic_parameters = management_parameters(manager, min(1, limit))
        rosters = tuple(tuple(r) for r in spec["case"]["rosters"])
        manager.project_many(rosters)
        actual_limit, actual_rosters, policy, adds = recorded.calls[0]
        assert actual_limit == limit and actual_rosters == rosters
        assert policy.streaming_slots == (2, 2)
        assert np.max(adds.sum(axis=-1)) <= limit
        if limit:
            assert (adds.sum(axis=(0, 1, 2)) > 0).all()
        else:
            assert not adds.any()
    finally:
        native.close()


@pytest.mark.parametrize("tactical", [False, True])
def test_marginal_boxes_equal_full_moments_and_follow_pool_changes(tactical):
    native = NativeKernel()
    try:
        manager, spec, _ = reference_manager(native, 1)
        if tactical:
            manager.pricing = PricingParameters(
                streaming_slots=2, upgrades=True, evidence=manager.parameters.evidence
            )
            manager.tactic_parameters = management_parameters(manager, 1)
        rosters = tuple(tuple(r) for r in spec["case"]["rosters"])
        boxes = manager.project_primary_boxes(rosters)
        assert not manager.controls  # A boxes request does not compute covariance corrections.
        assert boxes is manager.project_primary_boxes(rosters)
        full = manager.project_many(rosters)
        np.testing.assert_array_equal(boxes, full.boxes[:, 0])
        np.testing.assert_array_equal(manager.project_primary_boxes(rosters), full.boxes[:, 0])
        manager.set_pool(())
        assert not manager.box_cache
        boxes = manager.project_primary_boxes(rosters)
        primary = manager.project_primary(rosters)
        np.testing.assert_array_equal(boxes, primary.boxes)
        np.testing.assert_array_equal(manager.project_primary_boxes(rosters), primary.boxes)
    finally:
        native.close()


def managed_input(inputs):
    data = inputs.config.model.model_dump(mode="json")
    evidence = data["fit"]["evidence"]
    data["fit"]["steps"] = [0.00001, 0.25, 1.0]
    data.update(
        format_version=9,
        team_minutes={
            "regulation_minutes": 48,
            "players_on_court": 5,
            "overtime_minutes_per_game": 0,
            "unmodeled_reserve_minutes": 0,
            "evidence": evidence,
        },
        health={"injury_share": 0.5, "evidence": evidence},
        pricing={"streaming_slots": 1, "upgrades": True, "evidence": evidence},
    )
    model = ManagedPricingModel.model_validate_json(json.dumps(data))
    return inputs.model_copy(update={"config": inputs.config.model_copy(update={"model": model})})


def test_unquoted_candidate_gets_managed_value_without_inventing_a_quote(fitted_case):
    from fba.auction.auction import market_context, portfolio_for
    from fba.auction.fit import FittedUtility
    from fba.contracts.auction import ManagedFitSummary, Plan

    inputs, draft, kernel = fitted_case
    inputs = managed_input(inputs)
    inputs = inputs.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"projected_price": None}) if p.id == "007" else p
                for p in inputs.players
            )
        }
    )
    players, market = market_context(inputs, draft, "0" * 64)
    portfolio = portfolio_for(inputs, players, market, draft)
    base = portfolio.solve(canonical=True)
    assert isinstance(base, Plan) and base.players == ("005", "006")
    model = inputs.config.model
    fitted = FittedUtility(
        portfolio,
        market,
        draft.mine,
        model.fit,
        inputs.management,
        kernel,
        base,
        model.pricing,
        model.management,
    )
    mean, noise = fitted.context(fitted.anchor)
    gradient = fitted.gradient(mean, noise)
    features = fitted.marginals(base, gradient, mean, None)
    # Player 005 is the weaker purchase; compare full native season totals with
    # and without 007 in that seat, including the same opponent and waiver pool.
    rest = (fitted.manager.index["006"],)
    candidate = fitted.manager.index["007"]
    rivals = fitted.candidate_rivals(rest, candidate)
    without = fitted.manager.project_many((rest, *rivals)).boxes[:, 0]
    with_player = fitted.manager.project_many(((*rest, candidate), *rivals)).boxes[:, 0]
    expected = (with_player - without).mean(axis=(0, 1))
    assert np.linalg.norm(expected) > 0
    np.testing.assert_allclose(features[7], expected, rtol=0, atol=1e-12)
    result = calculate_auction(inputs, draft, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    assert isinstance(result.fit, ManagedFitSummary) and result.fit.selected_step > 0
    price = next(p for p in result.market.prices if p.player_id == "007")
    assert price.anchor is price.expected is price.acquisition is price.planning_cost is None
    cap = next(c for c in result.caps if c.player_id == "007")
    assert cap.conditional and cap.amount is not None
    assert isinstance(result.plan, Plan) and "007" not in result.plan.purchases


def test_complete_management_flows_through_auction_and_parallel_workers(fitted_case):
    inputs, draft, native = fitted_case
    inputs = managed_input(inputs)
    recorded = RecordedKernel(native)
    before = calculate_auction(inputs, draft, "0" * 64, "3" * 64, mode="fit", kernel=recorded)
    assert recorded.calls and all(len(c[1]) == inputs.config.league.teams for c in recorded.calls)
    for _, rosters, _, _ in recorded.calls:
        flattened = [i for r in rosters for i in r]
        assert len(flattened) == len(set(flattened))
    assert before.fit is not None and before.fit.selected_step == 0.00001
    session = AuctionSession(2)
    try:
        actual = calculate_auction(
            inputs,
            draft,
            "0" * 64,
            "3" * 64,
            mode="fit",
            kernel=native,
            runner=session.caps,
            feature_runner=session.features,
        )
        assert canonical(actual) == canonical(before)
    finally:
        session.close()


def test_desk_comparison_uses_selected_managed_utilities(fitted_case, tmp_path):
    from test_desk import wait_for

    from fba.apps.desk import AuctionDesk
    from fba.auction.auction import compare, portfolio_for
    from fba.contracts.auction import ManagedFitSummary, Plan
    from fba.contracts.desk import CompareRequest
    from fba.data.codec import digest

    inputs, draft, kernel = fitted_case
    inputs = managed_input(inputs)
    sha = digest(canonical(draft))
    fitted = calculate_auction(inputs, draft, "0" * 64, sha, mode="fit", kernel=kernel)
    assert isinstance(fitted.fit, ManagedFitSummary)
    raw = canonical(fitted)
    fitted = type(fitted).model_validate_json(raw)
    assert isinstance(fitted.fit, ManagedFitSummary)
    path = tmp_path / "draft.json"
    path.write_bytes(canonical(draft))
    service = AuctionDesk(
        inputs,
        "0" * 64,
        path,
        tmp_path / "log.jsonl",
        lambda d, s, cancelled: calculate_auction(inputs, d, "0" * 64, s),
        lambda d, s, cancelled: fitted,
    )
    try:
        wait_for(lambda: service.results().fit.status == "ready")
        utilities = {p.id: p.utility for p in fitted.fit.players}
        players = tuple(p.model_copy(update={"utility": utilities[p.id]}) for p in inputs.players)
        checked = 0
        for cap in fitted.caps:
            if cap.amount is None or cap.forced:
                continue
            legal = inputs.config.league.budget - 1
            for price in (max(1, cap.amount), cap.amount + 1):
                if not 1 <= price <= legal:
                    continue
                request = CompareRequest(
                    state_sha256=sha, mode="fit", player_id=cap.player_id, price=price
                )
                actual = service.comparison(request)
                portfolio = portfolio_for(inputs, players, fitted.market, draft)
                expected = compare(portfolio, cap.player_id, price)
                assert actual.mode == "fit" and actual.comparison == expected
                if isinstance(actual.comparison.buy, Plan) and isinstance(
                    actual.comparison.skip, Plan
                ):
                    if price <= cap.amount:
                        assert (
                            actual.comparison.delta >= -inputs.config.model.solver.value_tolerance
                        )
                    else:
                        assert actual.comparison.delta < inputs.config.model.solver.value_tolerance
                checked += 1
        assert checked
    finally:
        service.close()
