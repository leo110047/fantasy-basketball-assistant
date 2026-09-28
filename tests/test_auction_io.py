import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_preparation import annual_case as annual_case
from test_preparation import projection_bundle as projection_bundle

from fba.adapters.auction import auction_file, draft_template, load_auction, prepare_auction
from fba.adapters.codec import canonical, digest
from fba.adapters.preparation import project
from fba.contracts.auction import DraftState, MarketUpdate
from fba.contracts.base import ConfigError, DataError


@pytest.fixture
def frozen_auction(annual_case, tmp_path):
    snapshot, model, _, _ = annual_case
    projection = project(snapshot, model, tmp_path / "projection", None)
    frozen = prepare_auction(projection, model, tmp_path / "auction")
    return frozen / "auction-input.json", model, projection


def test_offline_cli_template_market_and_exact_rebuild(frozen_auction, tmp_path):
    path, model, projection = frozen_auction
    rebuilt = prepare_auction(projection, model, tmp_path / "rebuilt")
    assert path.read_bytes() == (rebuilt / path.name).read_bytes()
    draft = tmp_path / "draft.json"
    command = (sys.executable, "-m", "fba.apps.cli")
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    made = subprocess.run(
        (*command, "draft-template", str(path), "--mine", "1", "--output", str(draft)),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert made.returncode == 0, made.stderr
    state = DraftState.model_validate_json(draft.read_bytes())
    assert state.mine == "1" and len(state.teams) == 2 and state.sales == ()
    result = subprocess.run(
        (
            *command,
            "auction",
            str(path),
            "--draft",
            str(draft),
            "--stage",
            "market",
            "--workers",
            "1",
            "--output",
            str(tmp_path / "result"),
        ),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    published = Path(json.loads(result.stdout)["result"])
    market = MarketUpdate.model_validate_json(published.read_bytes())
    assert market.input_sha256 == digest(path.read_bytes())
    assert market.state_sha256 == digest(canonical(state))
    assert all(q.anchor is None for q in market.market.prices)
    with pytest.raises(DataError, match="cannot create draft"):
        draft_template(path, 1, draft)
    with pytest.raises(DataError, match="team number"):
        draft_template(path, 3, tmp_path / "invalid.json")
    with pytest.raises(DataError, match="stage"):
        auction_file(path, draft, tmp_path / "invalid", "other")


def test_auction_frozen_sources_and_derived_values_cannot_disagree(frozen_auction):
    path, _, _ = frozen_auction
    original = path.read_bytes()
    inputs, _ = load_auction(path)
    variants = (
        inputs.model_copy(update={"snapshot_sha256": "0" * 64}),
        inputs.model_copy(
            update={
                "players": (
                    inputs.players[0].model_copy(update={"fair": 999.0}),
                    *inputs.players[1:],
                )
            }
        ),
        inputs.model_copy(
            update={
                "management": inputs.management.model_copy(
                    update={
                        "players": (
                            inputs.management.players[0].model_copy(update={"expected_games": 0.0}),
                            *inputs.management.players[1:],
                        )
                    }
                )
            }
        ),
    )
    for changed in variants:
        path.write_bytes(canonical(changed))
        with pytest.raises(DataError, match="auction"):
            load_auction(path)
    path.write_bytes(original)
    (path.parent / "source/calculation.json").write_bytes(b"{}")
    with pytest.raises(DataError, match="SHA-256"):
        load_auction(path)


def test_auction_model_cannot_change_frozen_projection(frozen_auction, tmp_path):
    _, model, projection = frozen_auction
    changed = json.loads(model.read_text())
    changed["valuation"]["healthy_games"] += 1
    model.write_text(json.dumps(changed))
    with pytest.raises(ConfigError, match="valuation"):
        prepare_auction(projection, model, tmp_path / "changed")
    assert not (tmp_path / "changed").exists()


def test_auction_rejects_relabelled_frozen_statistic_axes(frozen_auction):
    path, _, _ = frozen_auction
    data = json.loads(path.read_bytes())
    axes = data["management"]["stat_ids"]
    axes[0], axes[1] = axes[1], axes[0]
    path.write_text(json.dumps(data))
    with pytest.raises(DataError, match="stat_ids"):
        load_auction(path)
