import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_preparation import annual_case as annual_case
from test_preparation import projection_bundle as projection_bundle

from fba.adapters.auction import auction_file, draft_template, load_auction, prepare_auction
from fba.adapters.preparation import project
from fba.contracts.auction import AuctionInput, DraftState, MarketUpdate
from fba.contracts.base import ConfigError, DataError
from fba.data.codec import canonical, digest


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


def test_distribution_market_rebuilds_from_an_unchanged_legacy_projection(annual_case, tmp_path):
    from test_market_distribution import sampled_parameters

    from fba.contracts.config import MarketParameters

    snapshot, model, _, _ = annual_case
    current = model.read_bytes()
    data = json.loads(current)
    data["market"] = sampled_parameters(
        MarketParameters.model_validate_json(json.dumps(data["market"]))
    ).model_dump(mode="json")
    model.write_text(json.dumps(data))
    projection = project(snapshot, model, tmp_path / "legacy-projection", None)
    with pytest.raises(ConfigError, match="prepare-auction"):
        prepare_auction(projection, model, tmp_path / "legacy-auction")
    assert not (tmp_path / "legacy-auction").exists()
    source = (projection / "projection-input.json").read_bytes()
    model.write_bytes(current)
    rebuilt = prepare_auction(projection, model, tmp_path / "distribution-auction")
    loaded, _ = load_auction(rebuilt / "auction-input.json")
    assert isinstance(loaded.config.model.market, MarketParameters)
    assert (projection / "projection-input.json").read_bytes() == source


def test_player_details_are_derived_from_frozen_sources_and_cannot_be_changed(frozen_auction):
    from fba.contracts.data import Snapshot
    from fba.contracts.projection import CalculationResult
    from fba.data.codec import decode

    path, _, _ = frozen_auction
    original = path.read_bytes()
    inputs, _ = load_auction(path)
    assert inputs.format_version == 4 and inputs.details is not None
    snapshot = decode(Snapshot, (path.parent / "source/snapshot.json").read_bytes(), "test")
    calculation = decode(
        CalculationResult, (path.parent / "source/calculation.json").read_bytes(), "test"
    )
    projected = {p.id: p for p in calculation.projections}
    for detail in inputs.details:
        row = next(p for p in snapshot.players if p.roster.id == detail.player_id)
        assert detail.team_id == row.team_id and detail.average_price == row.roster.average_price
        assert detail.forecast_sources == tuple(
            f.source_id for f in snapshot.forecasts if f.player_id == detail.player_id
        )
        assert detail.expected_games == projected[detail.player_id].expected_games
        assert detail.minutes == projected[detail.player_id].minutes
        assert tuple(s.value for s in detail.stats) == projected[detail.player_id].stats
    first, *rest = inputs.details
    for changed in ({"expected_games": 999.0}, {"forecast_sources": ()}, {"team_id": "changed"}):
        candidate = inputs.model_copy(update={"details": (first.model_copy(update=changed), *rest)})
        path.write_bytes(canonical(candidate))
        with pytest.raises(DataError, match="auction.details"):
            load_auction(path)
    path.write_bytes(original)


def test_legacy_input_does_not_silently_claim_source_metadata(frozen_auction):
    path, _, _ = frozen_auction
    current = json.loads(path.read_bytes())
    legacy = {**current, "format_version": 1}
    legacy.pop("details")
    legacy.pop("teams")
    legacy.pop("scenarios")
    path.write_text(json.dumps(legacy))
    loaded, source_hash = load_auction(path)
    assert source_hash == digest(path.read_bytes()) and loaded.details is None
    assert loaded.format_version == 1
    for invalid in ({**current, "details": None}, {**current, "format_version": 1}):
        with pytest.raises(ValueError, match="auction.details"):
            AuctionInput.model_validate_json(json.dumps(invalid))


def test_detail_source_gap_and_manual_notes_are_separate_facts(frozen_auction):
    from fba.adapters.auction import auction_details
    from fba.contracts.data import Adjustment, ManualAdjustments, Multiply, Provenance, Snapshot
    from fba.contracts.projection import CalculationResult
    from fba.data.codec import decode

    path, _, _ = frozen_auction
    inputs, _ = load_auction(path)
    snapshot = decode(Snapshot, (path.parent / "source/snapshot.json").read_bytes(), "test")
    calculation = decode(
        CalculationResult, (path.parent / "source/calculation.json").read_bytes(), "test"
    )
    pid = "1"  # Has individual history when the current forecast is absent.
    note = Adjustment(
        id="reviewed",
        player_id=pid,
        published_at=snapshot.as_of,
        effective_from=snapshot.as_of,
        reason="Manually checked the source gap; preserve current minutes",
        source="local note",
        assumption=True,
        operation=Multiply(kind="multiply", stat_id="MIN", factor=1.0),
    )
    provenance = Provenance(
        source_id="forecast",
        url="https://example.org/forecast",
        raw_sha256="1" * 64,
        available_as_of=snapshot.as_of,
        retrieved_at=snapshot.as_of,
        delivery="fetch",
    )
    changed = snapshot.model_copy(
        update={
            "forecasts": tuple(f for f in snapshot.forecasts if f.player_id != pid),
            "adjustments": ManualAdjustments(format_version=1, adjustments=(note,)),
            "artifacts": (
                snapshot.artifacts[0].model_copy(update={"provenance": provenance}),
                *snapshot.artifacts[1:],
            ),
        }
    )
    details = auction_details(changed, calculation, inputs.config)
    row = next(d for d in details if d.player_id == pid)
    assert row.forecast_sources == () and row.forecast_provenance == ()
    assert not row.forecast_usable and row.preparation_warnings
    assert row.adjustments == (note,)
    assert row.expected_games == next(
        p.expected_games for p in calculation.projections if p.id == pid
    )
    assert all(d.forecast_sources for d in details if d.player_id != pid)
    assert all(d.forecast_provenance == (provenance,) for d in details if d.player_id != pid)


def test_bootstrap_exposes_verified_player_details(frozen_auction, tmp_path):
    from fba.apps.desk import AuctionDesk
    from fba.auction.auction import calculate_auction

    path, _, _ = frozen_auction
    inputs, input_hash = load_auction(path)
    draft = tmp_path / "details-draft.json"
    draft_template(path, 1, draft)

    def calculate(state, state_hash, cancelled):
        return calculate_auction(inputs, state, input_hash, state_hash)

    desk = AuctionDesk(
        inputs, input_hash, draft, tmp_path / "details-log.jsonl", calculate, calculate
    )
    try:
        bootstrap = desk.bootstrap()
        assert bootstrap.details == inputs.details
        assert {d.player_id for d in bootstrap.details} == {p.id for p in bootstrap.players}
    finally:
        desk.close()


def test_auction_rejects_relabelled_frozen_statistic_axes(frozen_auction):
    path, _, _ = frozen_auction
    data = json.loads(path.read_bytes())
    axes = data["management"]["stat_ids"]
    axes[0], axes[1] = axes[1], axes[0]
    path.write_text(json.dumps(data))
    with pytest.raises(DataError, match="stat_ids"):
        load_auction(path)


@pytest.mark.parametrize("missing", ["games", "MIN"])
def test_unusable_current_forecast_keeps_source_and_preparation_warning(
    annual_case, tmp_path, missing
):
    from fba.adapters.snapshots import publish
    from fba.apps.desk import AuctionDesk
    from fba.auction.auction import calculate_auction

    root, model, _, snapshot = annual_case
    forecast = next(f for f in snapshot.forecasts if f.player_id == "1")
    replacement = forecast.model_copy(
        update={"expected_games": 0.0}
        if missing == "games"
        else {
            "totals": tuple(
                s.model_copy(update={"value": None, "missing_reason": "unavailable"})
                if s.id == missing
                else s
                for s in forecast.totals
            )
        }
    )
    changed = snapshot.model_copy(
        update={"forecasts": tuple(replacement if f == forecast else f for f in snapshot.forecasts)}
    )
    frozen = publish(
        changed,
        {a.path: (root / a.path).read_bytes() for a in snapshot.artifacts},
        tmp_path / "unusable-source",
    )
    projection = project(frozen, model, tmp_path / "unusable-projection", None)
    auction = prepare_auction(projection, model, tmp_path / "unusable-auction")
    inputs, sha = load_auction(auction / "auction-input.json")
    draft = tmp_path / "unusable-draft.json"
    draft_template(auction / "auction-input.json", 1, draft)

    def calculate(state, state_hash, cancelled):
        return calculate_auction(inputs, state, sha, state_hash)

    desk = AuctionDesk(inputs, sha, draft, tmp_path / "unusable-log.jsonl", calculate, calculate)
    try:
        row = next(d for d in desk.bootstrap().details if d.player_id == "1")
        assert row.forecast_sources == (forecast.source_id,)
        assert not row.forecast_usable
        reason = "nonpositive GP" if missing == "games" else "missing ['MIN']"
        assert any(reason in message for message in row.preparation_warnings)
        assert row.expected_games > 0  # Historical fallback is still valued, with a warning.
        assert next(d for d in inputs.details if d.player_id == "2").forecast_usable
    finally:
        desk.close()
