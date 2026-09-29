import json
from pathlib import Path

import numpy as np
import pytest
from test_auction import config
from test_fit import fitted_case as fitted_case

from fba.adapters.codec import canonical
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import ModelDocument, StreamingComparisonModel
from fba.contracts.season import ManagementPolicy
from fba.core.auction import calculate_auction, portfolio_for
from fba.core.config import validate_team_minutes
from fba.core.fit import FittedUtility
from fba.core.roster import effective_players
from fba.core.streaming import analyze_streaming, flex_candidates, select_streaming


def research_input(inputs, slots=(0, 1, 2), limit=None):
    data = json.loads((Path(__file__).parents[1] / "examples/2026-27/model.json").read_text())
    data.update(inputs.config.model.model_dump(mode="json", exclude={"format_version"}))
    data["format_version"] = 13
    data["streaming_comparison"]["slots"] = list(slots)
    league = inputs.config.league
    if limit is not None:
        league = league.model_copy(
            update={
                "transactions": league.transactions.model_copy(update={"adds_per_period": limit})
            }
        )
        data["management"]["reserve_adds"] = min(1, limit)
    model = ModelDocument.model_validate_json(json.dumps(data)).root
    assert isinstance(model, StreamingComparisonModel)
    return inputs.model_copy(
        update={"config": inputs.config.model_copy(update={"model": model, "league": league})}
    )


def test_recommendation_requires_every_paired_group_and_uses_configured_candidates():
    parameters = config().model.fit.model_copy(
        update={"health_blocks": 2, "improvement_tolerance": 0.0}
    )
    zero = np.zeros(4)
    # Central gain is positive, but one paired block loses. The larger option must be rejected.
    selected, comparisons = select_streaming(
        (0, 2, 5), (zero, np.ones(4), np.array([4.0, 4.0, 0.0, 0.0])), parameters
    )
    assert selected == 2
    assert comparisons[0].accepted
    assert comparisons[1].versus == 2 and not comparisons[1].accepted
    assert comparisons[1].paired_gain == 1 and comparisons[1].block_minimum == -1
    assert select_streaming((0, 2), (zero, zero), parameters)[0] == 0


@pytest.mark.parametrize("slots", [(1,), (0, 0), (0, 2, 1), (0, 3)])
def test_invalid_streaming_configuration_is_rejected(fitted_case, slots):
    inputs, _, _ = fitted_case
    research = research_input(inputs, slots)
    with pytest.raises(ConfigError, match="streaming_comparison.slots"):
        validate_team_minutes(research.config.model, research.config.season, research.config.league)


class CapturedKernel:
    def __init__(self, kernel):
        self.kernel, self.calls = kernel, []

    def run(self, arrays, rosters, pool, tactics, trace, *, primary_only=False):
        result = self.kernel.run(arrays, rosters, pool, tactics, trace, primary_only=primary_only)
        self.calls.append((arrays, rosters, pool, tactics.policy, result, primary_only))
        return result

    def __call__(self, *args):
        raise AssertionError("Streaming must use the shared tactical policy")


@pytest.mark.parametrize("limit", [0, 2, 5])
def test_streaming_uses_one_shared_configured_limit_and_paired_paths(fitted_case, limit):
    inputs, draft, kernel = fitted_case
    inputs = research_input(inputs, limit=limit)
    current = calculate_auction(inputs, draft, "0" * 64, "3" * 64)
    before = canonical(current)
    captured = CapturedKernel(kernel)
    result = analyze_streaming(inputs, draft, current, captured)
    assert result.shared_limit == limit and result.reserve_adds == min(1, limit)
    assert [r.slots for r in result.scenarios] == [0, 1, 2]
    assert result.health_samples == 4 and result.health_blocks == 2
    calls = [c for c in captured.calls if c[-1]]
    scenarios = calls[:3]
    assert [c[3].streaming_slots for c in scenarios] == [(0, 1), (1, 1), (2, 1)]
    for arrays, rosters, pool, policy, run, _ in scenarios:
        assert arrays is scenarios[0][0] and rosters == scenarios[0][1] and pool == scenarios[0][2]
        assert arrays.add_limit == limit and np.max(run.adds.sum(axis=-1)) <= limit
        assert policy.reserve_adds == min(1, limit)
        assert not (set(p for roster in rosters for p in roster) & set(pool))
        assert len({p for roster in rosters for p in roster}) == sum(map(len, rosters))
    if not limit:
        assert result.recommended_slots == 0 and not result.flex
        assert all(r.injury_adds + r.upgrade_adds + r.stream_adds == 0 for r in result.scenarios)
    assert canonical(current) == before


def test_legacy_input_requires_explicit_streaming_configuration(fitted_case):
    inputs, draft, kernel = fitted_case
    current = calculate_auction(inputs, draft, "0" * 64, "3" * 64)
    with pytest.raises(DataError, match="configured streaming"):
        analyze_streaming(inputs, draft, current, kernel)


def test_flex_candidates_use_recommended_policy_and_actual_removal_losses(fitted_case):
    inputs, draft, kernel = fitted_case
    inputs = research_input(inputs)
    current = calculate_auction(inputs, draft, "0" * 64, "3" * 64)
    players = effective_players(inputs.config.league, inputs.players, draft)
    portfolio = portfolio_for(inputs, players, current.market, draft)
    model = inputs.config.model
    captured = CapturedKernel(kernel)
    fitted = FittedUtility(
        portfolio,
        current.market,
        draft.mine,
        model.fit,
        inputs.management,
        captured,
        current.plan,
        model.pricing,
        model.management,
    )
    manager = fitted.manager
    mean, noise = fitted.context(fitted.anchor)
    gradient = fitted.gradient(mean, noise)
    policy = ManagementPolicy(
        streaming_slots=(2, 1), reserve_adds=model.management.reserve_adds, upgrades=True
    )
    arrays, tactics = manager.arrays(), manager.tactics(policy, model.management)
    run = kernel.run(
        arrays,
        (fitted.anchor, *fitted.opponent_rosters),
        manager.pool,
        tactics,
        False,
        primary_only=True,
    )
    realized, expected = manager.control_mean(fitted.anchor)
    baseline = (run.counts[:, 0] @ manager.raw - (realized - expected)).mean(axis=(0, 1))
    start = len(captured.calls)
    flex = flex_candidates(fitted, gradient, 2, arrays, tactics, baseline)
    removals = captured.calls[start:]
    assert len(removals) == len(fitted.anchor) == len(flex)
    independently = []
    for used_arrays, rosters, pool, used_policy, outcome, primary in removals:
        assert used_arrays is arrays and pool == manager.pool and primary
        assert used_policy.streaming_slots == (
            1,
            1,
        )  # Two requested seats capped by the one remaining player.
        missing = set(fitted.anchor) - set(rosters[0])
        assert len(missing) == 1
        removed_id = manager.ids[missing.pop()]
        real, expect = manager.control_mean(rosters[0])
        value = (outcome.counts[:, 0] @ manager.raw - (real - expect)).mean(axis=(0, 1))
        independently.append((float((baseline - value) @ gradient), removed_id))
    assert [(p.removal_loss, p.player_id) for p in flex] == sorted(independently)
