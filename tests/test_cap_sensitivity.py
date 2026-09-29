import itertools
import json
from concurrent.futures import CancelledError
from threading import Thread
from types import SimpleNamespace

import numpy as np
import pytest
from test_auction import independent_legal
from test_desk import desk as desk
from test_desk import request, sell_request, wait_for
from test_fit import fitted_case as fitted_case
from test_pricing_management import managed_input

from fba.adapters.codec import canonical
from fba.apps.desk import AuctionDesk
from fba.apps.server import DeskServer
from fba.contracts.auction import FittedPlayer, ManagedFitSummary
from fba.contracts.base import DataError
from fba.contracts.desk import SensitivityRequest
from fba.contracts.season import MarginalTask
from fba.core.auction import calculate_auction
from fba.core.fit import marginal_batch
from fba.core.sensitivity import cap_sensitivity


def test_marginal_groups_use_configured_partition_without_additional_simulations():
    calls = []
    boxes = np.array(
        [[[2.0, 4.0]], [[4.0, 8.0]], [[10.0, 20.0]], [[14.0, 28.0]], [[20.0, 40.0]], [[28.0, 56.0]]]
    )

    def project(rosters):
        calls.append(rosters)
        return boxes if len(rosters[0]) == 2 else np.zeros_like(boxes)

    manager = SimpleNamespace(
        parameters=SimpleNamespace(health_blocks=3), project_primary_boxes=project
    )
    (value,) = marginal_batch(
        manager, (MarginalTask(index=0, player=1, rest=(0,), opponents=((2,),)),)
    )
    assert len(calls) == 2
    assert value.values == (13.0, 26.0)
    assert value.blocks == ((3.0, 6.0), (12.0, 24.0), (24.0, 48.0))


@pytest.fixture
def managed_result(fitted_case):
    inputs, draft, kernel = fitted_case
    inputs = managed_input(inputs)
    result = calculate_auction(inputs, draft, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    assert isinstance(result.fit, ManagedFitSummary)
    assert result.fit.selected_step > 0
    assert len(result.fit.sensitivity) == inputs.config.model.fit.health_blocks
    return inputs, draft, result


def brute_cap(inputs, draft, market, sample, target):
    league = inputs.config.league
    prices = {p.player_id: p.planning_cost for p in market.prices}
    values = {p.id: p.utility for p in sample}
    size = len(league.starter_slots) + league.bench_slots
    lineups = [
        team
        for team in itertools.combinations(inputs.players, size)
        if independent_legal(league, team)
    ]
    skip = max(
        sum(values[p.id] for p in team)
        for team in lineups
        if all(p.id != target for p in team) and sum(prices[p.id] for p in team) <= league.budget
    )
    return max(
        (
            bid
            for bid in range(
                league.minimum_bid,
                league.budget - (size - 1) * league.minimum_bid + 1,
                league.bid_increment,
            )
            if any(
                target in {p.id for p in team}
                and sum(prices[p.id] for p in team if p.id != target) + bid <= league.budget
                and sum(values[p.id] for p in team)
                >= skip - inputs.config.model.solver.value_tolerance
                for team in lineups
            )
        ),
        default=0,
    )


def test_selected_cap_range_matches_independent_buy_skip_enumeration(managed_result):
    inputs, draft, result = managed_result
    samples = tuple(
        tuple(FittedPlayer(id=p.id, utility=float(sign * i)) for i, p in enumerate(inputs.players))
        for sign in (-1, 1)
    )
    result = result.model_copy(
        update={"fit": result.fit.model_copy(update={"sensitivity": samples})}
    )
    for player in (inputs.players[0], inputs.players[-1]):
        actual = cap_sensitivity(inputs, draft, result, player.id, lambda: None)
        expected = tuple(
            brute_cap(inputs, draft, result.market, sample, player.id) for sample in samples
        )
        assert actual.groups == expected
        assert actual.low == min(actual.central, *expected)
        assert actual.high == max(actual.central, *expected)
        assert actual.status == "ready" and actual.solver_calls > 0


def test_missing_stale_or_zero_step_groups_are_not_a_false_narrow_range(managed_result):
    inputs, draft, result = managed_result

    def changed(**updates):
        return result.model_copy(update={"fit": result.fit.model_copy(update=updates)})

    with pytest.raises(DataError, match="unavailable"):
        cap_sensitivity(
            inputs, draft, changed(sensitivity=None), inputs.players[0].id, lambda: None
        )
    with pytest.raises(DataError, match="count"):
        cap_sensitivity(inputs, draft, changed(sensitivity=()), inputs.players[0].id, lambda: None)
    samples = result.fit.sensitivity
    with pytest.raises(DataError, match="population"):
        cap_sensitivity(
            inputs,
            draft,
            changed(sensitivity=(samples[0][::-1], *samples[1:])),
            inputs.players[0].id,
            lambda: None,
        )
    baseline = cap_sensitivity(
        inputs,
        draft,
        changed(selected_step=0.0, sensitivity=()),
        inputs.players[0].id,
        lambda: None,
    )
    assert baseline.status == "baseline_retained" and baseline.low is None and baseline.high is None
    assert baseline.groups == () and baseline.solver_calls == 0

    def cancelled():
        raise CancelledError()

    with pytest.raises(CancelledError):
        cap_sensitivity(inputs, draft, result, inputs.players[0].id, cancelled)


def test_desk_sensitivity_rejects_stale_and_unmanaged_results(desk):
    sha = desk.results().state_sha256
    wait_for(lambda: desk.results().fit.status == "ready")
    request = SensitivityRequest(state_sha256=sha, player_id="000")
    with pytest.raises(DataError, match="unavailable"):
        desk.sensitivity(request)
    desk.save(sell_request(desk))
    with pytest.raises(DataError, match="another change"):
        desk.sensitivity(request)


def test_http_sensitivity_is_state_bound_logged_and_does_not_mutate_draft(managed_result, tmp_path):
    inputs, draft, result = managed_result
    path, log = tmp_path / "draft.json", tmp_path / "execution.jsonl"
    path.write_bytes(canonical(draft))

    def calculate(state, sha, cancelled):
        return result.model_copy(update={"state_sha256": sha})

    desk = AuctionDesk(inputs, "0" * 64, path, log, calculate, calculate)
    try:
        wait_for(lambda: desk.results().fit.status == "ready")
        payload = canonical(
            SensitivityRequest(
                state_sha256=desk.results().state_sha256, player_id=inputs.players[0].id
            )
        )
        before = path.read_bytes()
        with DeskServer(desk, 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                status, _, body = request(
                    server,
                    "POST",
                    "/api/sensitivity",
                    payload,
                    {"Content-Type": "application/json"},
                )
                assert status == 200 and json.loads(body)["sensitivity"]["status"] == "ready"
                assert path.read_bytes() == before
                records = [json.loads(line) for line in log.read_text().splitlines()]
                assert records[-1]["stage"] == "sensitivity"
                assert records[-1]["result"] == json.loads(body)
                assert (
                    request(
                        server,
                        "POST",
                        "/api/sensitivity",
                        payload,
                        {"Content-Type": "application/json", "Authorization": "invalid"},
                    )[0]
                    == 403
                )
                desk.save(sell_request(desk))
                assert (
                    request(
                        server,
                        "POST",
                        "/api/sensitivity",
                        payload,
                        {"Content-Type": "application/json"},
                    )[0]
                    == 400
                )
            finally:
                server.shutdown()
                thread.join()
    finally:
        desk.close()
