import json
from itertools import product
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_auction import config, independent_legal, inputs_for, player, state
from test_auction_io import frozen_auction as frozen_auction
from test_preparation import annual_case as annual_case
from test_preparation import projection_bundle as projection_bundle

from fba.adapters.paths import verdict
from fba.auction.auction import calculate_auction, market_context
from fba.auction.paths import AuctionPaths, clearing, own_bid
from fba.contracts.auction import DraftOverride, Sale
from fba.contracts.base import DataError
from fba.contracts.paths import StressSettings
from fba.core.roster import validate_draft
from fba.data.codec import canonical


def settings():
    return StressSettings.model_validate_json(
        (Path(__file__).parents[1] / "examples/2026-27/auction-stress.json").read_bytes()
    )


def case():
    c = config()
    league = c.league.model_copy(
        update={
            "teams": 2,
            "budget": 20,
            "minimum_bid": 2,
            "bid_increment": 2,
            "starter_slots": c.league.starter_slots[:2],
            "bench_slots": 0,
        }
    )
    players = tuple(
        player(i, ("PG", "SG"), utility=float(10 - i), quote=float(10 - i)) for i in range(8)
    )
    inputs = inputs_for(players, league)
    draft = state(inputs)
    current = calculate_auction(inputs, draft, "0" * 64, "3" * 64)
    return inputs, draft, current


@pytest.mark.parametrize(
    "bids,ties,expected",
    [
        ((0, 0), (0.1, 0.2), (None, 0)),
        ((8, 0), (0.1, 0.2), (0, 2)),
        ((8, 4), (0.1, 0.2), (0, 6)),
        ((8, 8), (0.2, 0.1), (1, 8)),
        ((8, 8), (0.1, 0.1), (0, 8)),
    ],
)
def test_clearing_respects_floor_increment_and_paired_ties(bids, ties, expected):
    inputs, _, _ = case()
    assert clearing(inputs.config.league, bids, ties) == expected


def test_full_configured_grid_is_legal_reproducible_and_order_independent():
    inputs, draft, current = case()
    before = canonical(draft)
    engine = AuctionPaths(inputs, draft, current, settings())
    pairs = engine.compare("000", 10)
    assert len(pairs) == 18
    assert {(p.regime, p.order, p.seed) for p in pairs} == set(
        product(settings().regimes, settings().orders, settings().seeds)
    )
    for pair in pairs:
        for branch in (pair.participate, pair.skip):
            final = draft.model_copy(update={"sales": (*draft.sales, *branch.sales)})
            assert validate_draft(inputs.config.league, engine.players, final) == branch.room
            assert len({s.player_id for s in final.sales}) == len(final.sales)
            for team in branch.room:
                assert team.budget >= team.slots * inputs.config.league.minimum_bid
                if not team.slots:
                    assert independent_legal(
                        inputs.config.league, tuple(p for p in engine.players if p.id in team.owned)
                    )
            own = next(t for t in branch.room if t.id == draft.mine)
            assert branch.score == sum(p.utility for p in inputs.players if p.id in own.owned)
            assert branch.complete == all(t.slots == 0 for t in branch.room)
        assert pair.delta == (
            pair.participate.score - pair.skip.score
            if pair.participate.complete and pair.skip.complete
            else None
        )
    shuffled = inputs.model_copy(update={"players": inputs.players[::-1]})
    draft = draft.model_copy(update={"teams": draft.teams[::-1]})
    assert AuctionPaths(shuffled, draft, current, settings()).compare("000", 10) == pairs
    assert before == canonical(draft.model_copy(update={"teams": draft.teams[::-1]}))


def test_private_settings_cannot_change_a_public_bid_or_a_skip_first_sale():
    inputs, draft, current = case()
    engine = AuctionPaths(inputs, draft, current, settings())
    players, market = market_context(inputs, draft, "0" * 64)
    first = own_bid(inputs, players, draft, market, 0, engine.eligible)
    alternative = settings().model_copy(
        update={"premium_multiplier": 8.0, "seeds": (42,), "orders": ("mixed",)}
    )
    changed = AuctionPaths(inputs, draft, current, alternative)
    assert changed.public_bid(0, 1, 2, 1, True, draft, market.room) == first
    # Changing only the participate ceiling leaves all skip traces exactly unchanged.
    a, b = engine.compare("000", 2), engine.compare("000", 16)
    assert tuple(p.skip for p in a) == tuple(p.skip for p in b)
    assert any(p.participate != q.participate for p, q in zip(a, b, strict=True))


def test_skip_is_only_current_nomination_and_can_buy_in_second_sweep():
    inputs, draft, _ = case()
    sold = (
        Sale(id="a", player_id="000", buyer="team-01", amount=2),
        Sale(id="b", player_id="001", buyer="team-01", amount=2),
        Sale(id="c", player_id="002", buyer="team-00", amount=2),
    )
    inputs = inputs.model_copy(update={"players": inputs.players[:4]})
    draft = draft.model_copy(update={"sales": sold})
    current = calculate_auction(inputs, draft, "0" * 64, "3" * 64)
    engine = AuctionPaths(inputs, draft, current, settings())
    result = engine.continuation(3, 6, "market", "market", 42, False)
    assert result.first_sale is None and result.complete
    assert result.sales[0].player_id == "003" and result.sales[0].amount == 2
    assert [b.amount for b in result.bids] == [0, 18]
    once = AuctionPaths(inputs, draft, current, settings().model_copy(update={"maximum_sweeps": 1}))
    pairs = once.compare("003", 6)
    assert all(p.delta is None for p in pairs)
    assert verdict(pairs, 1e-7) == "incomplete"


def test_unknown_quotes_and_unconfirmed_positions_do_not_become_dollar_floor_sales():
    inputs, draft, current = case()
    players = (
        *inputs.players[:6],
        inputs.players[6].model_copy(update={"projected_price": None}),
        inputs.players[7].model_copy(update={"positions_confirmed": False}),
    )
    engine = AuctionPaths(
        inputs.model_copy(update={"players": players}), draft, current, settings()
    )
    assert {engine.players[i].id for i in engine.pool} == {p.id for p in players[:6]}
    for p in ("006", "007"):
        with pytest.raises(DataError, match="unavailable"):
            engine.compare(p, 2)
    draft = draft.model_copy(
        update={
            "overrides": (
                DraftOverride(player_id="006", market=2.0, positions=None, reason="observed quote"),
            )
        }
    )
    assert (
        6
        in AuctionPaths(
            inputs.model_copy(update={"players": players}), draft, current, settings()
        ).pool
    )


@pytest.mark.parametrize("key", ["regimes", "orders", "seeds"])
def test_duplicate_scenario_settings_are_rejected(key):
    raw = settings().model_dump(mode="json")
    raw[key] = [raw[key][0], raw[key][0]]
    with pytest.raises(ValidationError, match="duplicate scenario"):
        StressSettings.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("ceiling", [0, 3, 20])
def test_illegal_initial_bid_is_rejected(ceiling):
    inputs, draft, current = case()
    with pytest.raises(DataError, match="legal bid"):
        AuctionPaths(inputs, draft, current, settings()).compare("000", ceiling)


def test_paths_freeze_selected_managed_utilities():
    from fba.contracts.auction import FittedPlayer, ManagedFitSummary
    from fba.contracts.config import PricingParameters

    inputs, draft, current = case()
    utility = tuple(FittedPlayer(id=p.id, utility=float(i)) for i, p in enumerate(inputs.players))
    summary = ManagedFitSummary(
        anchor=current.plan.players,
        selected_step=0.1,
        steps=(),
        samples=4,
        health_samples=4,
        method="paired_managed_marginal",
        players=utility,
        policy=PricingParameters(streaming_slots=0, upgrades=False, evidence=settings().evidence),
    )
    fitted = current.model_copy(update={"fit": summary})
    engine = AuctionPaths(inputs, draft, fitted, settings())
    assert {p.id: p.utility for p in engine.players} == {p.id: p.utility for p in utility}
    for pair in engine.compare("000", 10):
        for branch in (pair.participate, pair.skip):
            own = next(t for t in branch.room if t.id == draft.mine)
            assert branch.score == sum(p.utility for p in utility if p.id in own.owned)


def test_paths_cli_publishes_bound_result_without_changing_draft(frozen_auction, tmp_path):
    import os
    import subprocess
    import sys

    from fba.adapters.auction import draft_template, load_auction
    from fba.contracts.auction import DraftState
    from fba.contracts.paths import StressResult
    from fba.data.codec import digest

    path, _, _ = frozen_auction
    inputs, input_hash = load_auction(path)
    draft_path = draft_template(path, 1, tmp_path / "draft.json")
    draft = DraftState.model_validate_json(draft_path.read_bytes())
    draft = draft.model_copy(
        update={
            "overrides": tuple(
                DraftOverride(
                    player_id=p.id,
                    market=float(inputs.config.league.minimum_bid),
                    positions=None,
                    reason="test quote",
                )
                for p in inputs.players
            )
        }
    )
    draft_path.write_bytes(canonical(draft))
    before = draft_path.read_bytes()
    specification = settings().model_copy(
        update={
            "evidence": settings().evidence.model_copy(
                update={"as_of": inputs.config.season.snapshot_as_of}
            )
        }
    )
    spec = tmp_path / "stress.json"
    spec.write_bytes(canonical(specification))
    run = subprocess.run(
        (
            sys.executable,
            "-m",
            "fba.apps.cli",
            "auction-paths",
            str(path),
            "--draft",
            str(draft_path),
            "--settings",
            str(spec),
            "--output",
            str(tmp_path / "results"),
            "--player",
            inputs.players[0].id,
            "--ceiling",
            str(inputs.config.league.minimum_bid),
            "--mode",
            "equal",
            "--workers",
            "1",
        ),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    published = Path(json.loads(run.stdout)["result"])
    result = StressResult.model_validate_json(published.read_bytes())
    assert published.stem == "auction-paths-" + digest(canonical(result))
    assert result.input_sha256 == input_hash and result.state_sha256 == digest(before)
    assert result.config == draft.config and result.settings_sha256 == digest(
        canonical(specification)
    )
    assert len(result.pairs) == 18 and result.calculation_seconds > 0 and result.paths_seconds > 0
    assert result.solver_calls >= sum(
        p.participate.solver_calls + p.skip.solver_calls for p in result.pairs
    )
    assert result.state == draft and result.initial.state_sha256 == result.state_sha256
    assert result.initial.input_sha256 == result.input_sha256
    assert draft_path.read_bytes() == before
