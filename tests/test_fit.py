import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
from test_auction import config, inputs_for, player, state

from fba.adapters.codec import canonical
from fba.adapters.native import NativeKernel
from fba.contracts.auction import AuctionPlayer, Infeasible, Plan, Sale
from fba.contracts.base import DataError
from fba.contracts.season import ManagedPlayer, ManagementInput
from fba.core.auction import calculate_auction, market_context, portfolio_for
from fba.core.fit import FittedUtility, categories, opponent_rosters


@pytest.fixture
def fitted_case():
    c = config()
    league = c.league.model_copy(
        update={"teams": 2, "starter_slots": c.league.starter_slots[:1], "bench_slots": 1}
    )
    parameters = c.model.fit.model_copy(
        update={"samples": 64, "health_samples": 4, "health_blocks": 2, "steps": (0.25, 1.0)}
    )
    players = tuple(player(i, utility=float(i + 1), quote=0.0) for i in range(8))
    days = tuple(date(2026, 10, 20) + timedelta(days=i) for i in range(7))
    stat_ids = (*c.model.projection.stat_ids, c.model.projection.threshold_stat)
    rows = tuple(
        ManagedPlayer(
            id=p.id,
            expected_games=5.0,
            healthy_games=6.0,
            season_games=7.0,
            return_on=None,
            game_days=days,
            means=tuple(float(i + 1) for _ in stat_ids),
            covariance=tuple(tuple(row) for row in np.eye(len(stat_ids))),
        )
        for i, p in enumerate(players)
    )
    inp = inputs_for(players, league).model_copy(
        update={
            "management": ManagementInput(
                stat_ids=stat_ids, sampling_ids=tuple(p.id for p in rows), players=rows
            )
        }
    )
    inp = inp.model_copy(
        update={
            "config": inp.config.model_copy(
                update={"model": c.model.model_copy(update={"fit": parameters})}
            )
        }
    )
    kernel = NativeKernel()
    yield inp, state(inp), kernel
    kernel.close()


def test_fit_runs_shared_kernel_and_preserves_shuffle_and_parallel_results(fitted_case):
    from fba.apps.auction import AuctionSession

    inputs, draft, kernel = fitted_case
    baseline = calculate_auction(inputs, draft, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    assert isinstance(baseline.plan, Plan) and baseline.fit is not None
    shuffled = inputs.model_copy(
        update={
            "players": tuple(reversed(inputs.players)),
            "management": inputs.management.model_copy(
                update={"players": tuple(reversed(inputs.management.players))}
            ),
        }
    )
    assert canonical(baseline) == canonical(
        calculate_auction(shuffled, draft, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    )
    session = AuctionSession(2)
    try:
        actual = calculate_auction(
            inputs,
            draft,
            "0" * 64,
            "3" * 64,
            mode="fit",
            kernel=kernel,
            runner=session.caps,
            feature_runner=session.features,
        )
        assert canonical(actual) == canonical(baseline)
    finally:
        session.close()


@pytest.mark.parametrize("paired_gain", [False, True])
def test_fit_noise_guard_requires_central_and_each_paired_block_gain(
    fitted_case, monkeypatch, paired_gain
):
    inputs, draft, kernel = fitted_case
    scores = iter((0.5, 0.7, 0.9))
    blocks = iter(
        (
            np.array([0.5, 0.5]),
            np.array([0.8, 0.6 if paired_gain else 0.4]),
            np.array([0.9, 0.7 if paired_gain else 0.3]),
        )
    )
    monkeypatch.setattr(
        FittedUtility, "gradient", lambda self, mean, noise: np.ones(self.manager.k)
    )
    monkeypatch.setattr(FittedUtility, "score", lambda *args: next(scores))
    monkeypatch.setattr(FittedUtility, "block_scores", lambda *args: next(blocks))
    result = calculate_auction(inputs, draft, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    assert result.fit.selected_step == (1.0 if paired_gain else 0.0)
    assert all(step.accepted == paired_gain for step in result.fit.steps[1:])
    # The historical max-central-score policy selects the last, noisy proposal.
    assert max(result.fit.steps, key=lambda step: step.score).step == 1.0


def test_fit_missing_kernel_or_uncompletable_benchmark_is_explicit(fitted_case):
    inputs, draft, _ = fitted_case
    with pytest.raises(DataError, match="kernel"):
        calculate_auction(inputs, draft, "0" * 64, "3" * 64, mode="fit")
    players, market = market_context(inputs, draft, "0" * 64)
    p = portfolio_for(inputs, players[:2], market, draft)
    with pytest.raises(DataError, match="benchmark"):
        opponent_rosters(p, market, draft.mine, tuple(p.id for p in players[:2]))


def test_category_direction_zero_denominator_and_empty_buy_branch(fitted_case):
    from fba.contracts.config import Category, Ratio, Term

    inputs, draft, _ = fitted_case
    term = Term(stat_id="x", coefficient=1.0)
    ratio = Ratio(kind="ratio", numerator=(term,), denominator=(term,), zero_denominator="error")
    c = Category(
        id="x", label="x", formula=ratio, direction="lower", comparison_decimals=6, tie_value=0.5
    )
    league = inputs.config.league.model_copy(update={"categories": (c,)})
    assert categories(np.ones((2, 1)), league, ("x",)).tolist() == [[-1.0], [-1.0]]
    with pytest.raises(DataError, match="zero denominator"):
        categories(np.zeros((1, 1)), league, ("x",))
    # An absent priced population is an explicit infeasible plan, not a zero-filled one.
    absent = inputs.model_copy(
        update={
            "players": tuple(p.model_copy(update={"projected_price": None}) for p in inputs.players)
        }
    )
    result = calculate_auction(absent, draft, "0" * 64, "3" * 64)
    assert isinstance(result.plan, Infeasible)
    assert all(c.amount is None for c in result.caps)


def test_fit_failure_contracts_and_full_opponent_roster(fitted_case, monkeypatch):
    from fba.contracts.auction import Sale, SolverError
    from fba.core.portfolio import Portfolio

    inputs, draft, kernel = fitted_case
    sales = tuple(Sale(id=str(i), player_id=f"{i:03}", buyer="team-01", amount=1) for i in range(2))
    filled = draft.model_copy(update={"sales": sales})
    result = calculate_auction(inputs, filled, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    assert isinstance(result.plan, Plan)
    no_projection = inputs.model_copy(
        update={
            "players": (
                inputs.players[0].model_copy(update={"utility": None, "fair": None}),
                *inputs.players[1:],
            )
        }
    )
    with pytest.raises(DataError, match="observed purchase"):
        calculate_auction(no_projection, filled, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    invalid_samples = inputs.model_copy(
        update={
            "config": inputs.config.model_copy(
                update={
                    "model": inputs.config.model.model_copy(
                        update={"fit": inputs.config.model.fit.model_copy(update={"samples": 63})}
                    )
                }
            )
        }
    )
    with pytest.raises(DataError, match="power of two"):
        calculate_auction(invalid_samples, draft, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    with monkeypatch.context() as patch:
        patch.setattr(
            FittedUtility,
            "marginals",
            lambda self, *args: np.zeros((len(self.portfolio.players), self.manager.k)),
        )
        with pytest.raises(DataError, match="no variation"):
            calculate_auction(inputs, draft, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    original = Portfolio.solve
    calls = 0

    def broken_proposal(self, **kwargs):
        nonlocal calls
        calls += 1
        return (
            original(self, **kwargs)
            if calls == 1
            else Infeasible(reason="injected solver inconsistency")
        )

    monkeypatch.setattr(Portfolio, "solve", broken_proposal)
    with pytest.raises(SolverError, match="feasibility"):
        calculate_auction(inputs, draft, "0" * 64, "3" * 64, mode="fit", kernel=kernel)


def test_fit_owned_and_unavailable_players_and_completed_roster(fitted_case):
    from fba.contracts.auction import Sale

    inputs, draft, kernel = fitted_case
    inputs = inputs.model_copy(
        update={
            "players": (
                *inputs.players[:-1],
                inputs.players[-1].model_copy(update={"fair": None, "utility": None}),
            )
        }
    )
    bought = tuple(
        Sale(id=str(i), player_id=f"{i:03}", buyer=draft.mine, amount=1) for i in range(2)
    )
    partial = draft.model_copy(update={"sales": bought[:1]})
    result = calculate_auction(inputs, partial, "0" * 64, "3" * 64, mode="fit", kernel=kernel)
    assert result.fit is not None
    full = calculate_auction(
        inputs,
        draft.model_copy(update={"sales": bought}),
        "0" * 64,
        "3" * 64,
        mode="fit",
        kernel=kernel,
    )
    assert full.fit is None and full.plan.purchases == ()


def test_joint_statistic_draws_have_independent_own_and_opponent_axes(fitted_case):
    inputs, draft, kernel = fitted_case
    players, market = market_context(inputs, draft, "0" * 64)
    portfolio = portfolio_for(inputs, players, market, draft)
    base = portfolio.solve(canonical=True)
    parameters = inputs.config.model.fit.model_copy(update={"samples": 8192})
    fitted = FittedUtility(
        portfolio, market, draft.mine, parameters, inputs.management, kernel, base
    )
    k = fitted.manager.k
    correlation = np.corrcoef(fitted.draws.T, fitted.opponent_draws.T)[:k, k:]
    assert np.abs(correlation).max() < 0.02
    np.testing.assert_allclose(fitted.draws.std(axis=0), 1.0, atol=0.002)
    np.testing.assert_allclose(fitted.opponent_draws.std(axis=0), 1.0, atol=0.002)
    repeated = FittedUtility(
        portfolio, market, draft.mine, parameters, inputs.management, kernel, base
    )
    np.testing.assert_array_equal(fitted.draws, repeated.draws)
    np.testing.assert_array_equal(fitted.opponent_draws, repeated.opponent_draws)
    for field in ("seed", "opponent_seed"):
        changed = FittedUtility(
            portfolio,
            market,
            draft.mine,
            parameters.model_copy(update={field: getattr(parameters, field) + 1}),
            inputs.management,
            kernel,
            base,
        )
        assert not np.array_equal(fitted.draws, changed.draws)
        assert not np.array_equal(fitted.opponent_draws, changed.opponent_draws)


def test_fitted_cap_seed_bound_on_frozen_draft_states():
    from fba.apps.auction import AuctionSession

    fixture = json.loads((Path(__file__).parent / "fixtures/fit-stability.json").read_bytes())
    players = tuple(AuctionPlayer.model_validate_json(json.dumps(p)) for p in fixture["players"])
    inputs = inputs_for(players).model_copy(
        update={
            "management": ManagementInput.model_validate_json(json.dumps(fixture["management"]))
        }
    )
    sales = tuple(Sale.model_validate(s) for s in fixture["sales"])
    selected = []
    session = AuctionSession(4)
    try:
        for count in (0, 20, 80):
            reference = None
            for offset in (0, 1, 2, 3):
                parameters = inputs.config.model.fit
                model = inputs.config.model.model_copy(
                    update={
                        "fit": parameters.model_copy(
                            update={
                                field: getattr(parameters, field) + offset
                                for field in ("seed", "opponent_seed", "health_seed")
                            }
                        )
                    }
                )
                candidate = inputs.model_copy(
                    update={"config": inputs.config.model_copy(update={"model": model})}
                )
                result = calculate_auction(
                    candidate,
                    state(candidate, sales[:count]),
                    "0" * 64,
                    "3" * 64,
                    mode="fit",
                    kernel=session.native(),
                    runner=session.caps,
                    feature_runner=session.features,
                )
                assert isinstance(result.plan, Plan) and result.fit is not None
                selected.append(result.fit.selected_step)
                caps = {c.player_id: c.amount for c in result.caps}
                if reference is None:
                    reference = caps
                for pid, amount in caps.items():
                    prior = reference[pid]
                    assert (amount is None) == (prior is None)
                    if amount is not None:
                        assert abs(amount - prior) <= 2
        assert any(selected), "The stability case must exercise an accepted fit update"
    finally:
        session.close()
