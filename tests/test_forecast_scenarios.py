import json
from pathlib import Path

import pytest
from test_auction_io import frozen_auction as frozen_auction
from test_calculation import projection_bundle as projection_bundle
from test_preparation import adjustment
from test_preparation import annual_case as annual_case

from fba.adapters.auction import load_auction, prepare_auction
from fba.adapters.forecast_scenarios import freeze_scenarios, scenario_paths, scenario_prices
from fba.adapters.preparation import project
from fba.adapters.snapshots import artifact, publish
from fba.contracts.base import DataError
from fba.contracts.data import ExpectedGames, ManualAdjustments
from fba.contracts.projection import CalculationResult
from fba.data.codec import canonical, digest


def test_scenarios_freeze_named_values_without_changing_central_input(frozen_auction, tmp_path):
    path, model, projection = frozen_auction
    before, _ = load_auction(path)
    result = prepare_auction(
        projection, model, tmp_path / "scenarios", (("Same source", projection),)
    )
    after, _ = load_auction(result / "auction-input.json")
    assert after.players == before.players
    assert after.management == before.management and after.details == before.details
    assert after.config == before.config and after.snapshot_sha256 == before.snapshot_sha256
    assert [s.name for s in after.scenarios] == ["Same source"]
    assert {p.player_id: p.fair for p in after.scenarios[0].prices} == {
        p.id: p.fair for p in after.players
    }
    # Reading the frozen auction does not depend on the original projection directory.
    projection.rename(projection.with_name("moved-projection"))
    assert load_auction(result / "auction-input.json")[0] == after
    legacy = after.model_copy(update={"format_version": 3, "scenarios": None})
    (result / "auction-input.json").write_bytes(canonical(legacy))
    assert load_auction(result / "auction-input.json")[0].scenarios is None


def test_scenario_changes_are_rejected_even_when_manifest_bytes_are_valid(frozen_auction, tmp_path):
    _, model, projection = frozen_auction
    root = prepare_auction(projection, model, tmp_path / "scenarios", (("Variant", projection),))
    path = root / "auction-input.json"
    original, _ = load_auction(path)
    scenario = original.scenarios[0]
    changed = scenario.model_copy(
        update={
            "prices": (scenario.prices[0].model_copy(update={"fair": 999.0}), *scenario.prices[1:])
        }
    )
    result_path, _, _ = scenario_paths(scenario)
    variants = (
        {"scenarios": (changed,)},
        {"scenarios": (scenario, scenario)},
        {"scenarios": (scenario.model_copy(update={"projection_sha256": "0" * 64}),)},
        {"artifacts": tuple(a for a in original.artifacts if a.path != result_path)},
    )
    for variant in variants:
        path.write_bytes(canonical(original.model_copy(update=variant)))
        with pytest.raises(DataError, match="auction.scenarios"):
            load_auction(path)
    path.write_bytes(canonical(original))
    (root / result_path).write_bytes((root / result_path).read_bytes() + b" ")
    with pytest.raises(DataError, match="SHA-256"):
        load_auction(path)


def test_scenario_source_requires_same_snapshot_and_complete_result(frozen_auction, tmp_path):
    path, model, projection = frozen_auction
    inputs, _ = load_auction(path)
    with pytest.raises(DataError, match="same league, season and snapshot"):
        freeze_scenarios((("Wrong snapshot", projection),), inputs.config, "0" * 64, inputs.players)
    for sources in ((("Name", projection), ("Name", projection)), ((" ", projection),)):
        with pytest.raises(DataError, match="names"):
            prepare_auction(projection, model, tmp_path / "invalid", sources)
    result_path = next((projection / "results").glob("calculation-*.json"))
    payload = result_path.read_bytes()
    wrong = CalculationResult.model_validate_json(payload).model_copy(
        update={"input_sha256": "0" * 64}
    )
    changed = canonical(wrong)
    result_path.unlink()
    (result_path.parent / f"calculation-{digest(changed)}.json").write_bytes(changed)
    with pytest.raises(DataError, match="linkage"):
        prepare_auction(projection, model, tmp_path / "invalid")
    with pytest.raises(DataError, match="populations"):
        scenario_prices(
            wrong.model_copy(
                update={"valuation": wrong.valuation.model_copy(update={"players": ()})}
            ),
            inputs.players,
        )
    assert not (tmp_path / "invalid").exists()


def test_alternate_model_result_and_cli_scenario_names_are_preserved(
    annual_case, frozen_auction, tmp_path
):
    from fba.apps.cli import parser

    snapshot, model, _, _ = annual_case
    _, _, central = frozen_auction
    changed = json.loads(model.read_bytes())
    changed["valuation"]["replacement_count"] = 1
    alternate_model = tmp_path / "alternate-model.json"
    alternate_model.write_text(json.dumps(changed))
    alternate = project(snapshot, alternate_model, tmp_path / "alternate", None)
    root = prepare_auction(central, model, tmp_path / "compare", (("Replacement 1", alternate),))
    inputs, _ = load_auction(root / "auction-input.json")
    assert inputs.scenarios[0].model != inputs.config.refs.model
    parsed = parser().parse_args(
        [
            "prepare-auction",
            str(central),
            "--model",
            str(model),
            "--output",
            str(tmp_path / "cli"),
            "--scenario",
            "A with spaces",
            str(alternate),
            "--scenario",
            "B",
            str(central),
        ]
    )
    assert tuple((name, Path(path)) for name, path in parsed.scenario) == (
        ("A with spaces", alternate),
        ("B", central),
    )


def test_reload_rejects_complete_scenario_transplanted_from_another_snapshot(
    annual_case, frozen_auction, tmp_path
):
    root, model, _, snapshot = annual_case
    path, _, _ = frozen_auction
    inputs, _ = load_auction(path)
    change = adjustment(snapshot, "0", ExpectedGames(kind="expected_games", games=2.0), "case")
    changed = snapshot.model_copy(
        update={"adjustments": ManualAdjustments(format_version=1, adjustments=(change,))}
    )
    frozen = publish(
        changed,
        {a.path: (root / a.path).read_bytes() for a in snapshot.artifacts},
        tmp_path / "another-snapshot",
    )
    projection = project(frozen, model, tmp_path / "another-projection", None)
    rows, files = freeze_scenarios(
        (("Transplanted", projection),),
        inputs.config,
        digest((frozen / "snapshot.json").read_bytes()),
        inputs.players,
    )
    for name, payload in files.items():
        destination = path.parent / name
        destination.parent.mkdir(exist_ok=True, parents=True)
        destination.write_bytes(payload)
    candidate = inputs.model_copy(
        update={
            "scenarios": rows,
            "artifacts": (
                *inputs.artifacts,
                *(artifact(name, data) for name, data in files.items()),
            ),
        }
    )
    path.write_bytes(canonical(candidate))
    with pytest.raises(DataError, match="different snapshot"):
        load_auction(path)
