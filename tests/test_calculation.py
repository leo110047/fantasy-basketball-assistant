import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from fba.adapters.calculation import calculate_file, load_calculation_input
from fba.adapters.codec import canonical, digest
from fba.adapters.config import load_config
from fba.adapters.migration import migrate_projection
from fba.adapters.snapshots import artifact, publish
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import PreparationModel
from fba.contracts.data import CalibrationPair, ManualAdjustments, Snapshot
from fba.contracts.projection import CalibratedInput
from fba.core.data import fit_availability


@pytest.fixture
def projection_bundle(tmp_path):
    root = Path(__file__).parents[1]
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    files = {
        name: json.loads((root / f"examples/2026-27/{name}.json").read_text())
        for name in ("league", "season", "model")
    }
    league = files["league"]
    league["teams"] = 2
    league["starter_slots"] = league["starter_slots"][:1]
    league["bench_slots"] = 1
    league["playoffs"]["team_count"] = 2
    league["playoffs"]["byes"] = 0
    league["playoffs"]["week_ids"] = ["w20"]
    for week in league["matchups"]:
        if week["id"] in ("w21", "w22"):
            week["phase"] = "postseason"
    league["categories"] = [next(c for c in league["categories"] if c["id"] == "PTS")]
    files["model"]["valuation"]["healthy_games"] = 3
    files["model"]["valuation"]["replacement_count"] = 2
    for name, value in files.items():
        (config_dir / f"{name}.json").write_text(json.dumps(value))
    effective = load_config(*(config_dir / f"{name}.json" for name in files))
    players = []
    for i in range(12):
        stats = [
            2 + i / 10,
            6 + i / 10,
            1.0,
            2.0,
            6 + i / 5,
            1.0,
            3 + i / 10,
            1.0,
            2.0,
            1.0,
            0.5,
            0.5,
        ]
        players.append(
            {
                "id": str(i),
                "name": str(i),
                "team_id": "A" if i < 6 else "B",
                "priors": [{"id": "a", "expected_games": 3.0, "minutes": 15.0, "stats": stats}],
                "games_cap": None,
                "return_on": None,
                "history": [stats],
            }
        )
    dates = [str(date(2026, 10, 20) + timedelta(days=i * 2)) for i in range(3)]
    pairs = tuple(
        CalibrationPair(player_id=str(i), projected_games=g, actual_games=g // 2)
        for i, g in enumerate((2, 4))
    )
    pairs_data = json.dumps([p.model_dump() for p in pairs]).encode()
    calibration = fit_availability(
        pairs,
        effective.season.previous_season_id,
        (digest(pairs_data),),
        effective.model.calibration.value,
    )
    snapshot = Snapshot(
        format_version=1,
        season_id=effective.season.season_id,
        version=1,
        as_of=effective.season.snapshot_as_of,
        config=effective.refs,
        artifacts=(artifact("pairs.json", pairs_data),),
        players=(),
        schedule=(),
        schedule_counts=(),
        forecasts=(),
        history=(),
        calibration=calibration,
        adjustments=ManualAdjustments(format_version=1, adjustments=()),
    )
    frozen = publish(snapshot, {"pairs.json": pairs_data}, tmp_path / "calibration")
    data = {
        "format_version": 3,
        "calibration": calibration.model_dump(mode="json"),
        "calibration_snapshot": str(frozen.relative_to(tmp_path)),
        "config": effective.model_dump(mode="json"),
        "players": players,
        "teams": [{"id": team, "dates": dates, "full_season_games": 4} for team in ("A", "B")],
        "artifacts": [
            {
                "path": str(p.relative_to(tmp_path)),
                "sha256": digest(p.read_bytes()),
                "size": p.stat().st_size,
                "provenance": None,
            }
            for p in (*config_dir.glob("*.json"), *frozen.glob("*.json"))
        ],
    }
    path = tmp_path / "projection-input.json"
    path.write_bytes(canonical(CalibratedInput.model_validate_json(json.dumps(data))))
    return path


def test_calculation_rebuild_is_exact_and_does_not_overwrite(projection_bundle, tmp_path):
    path = projection_bundle
    one = calculate_file(path, tmp_path / "one")
    two = calculate_file(path, tmp_path / "two")
    assert one.read_bytes() == two.read_bytes()
    result = json.loads(one.read_text())
    assert result["input_sha256"] == digest(path.read_bytes())
    assert all(p["expected_games"] == 1.5 for p in result["projections"])
    assert set(result["config"]) == {"league", "season", "model"}
    assert sum(
        sorted((p["fair"] for p in result["valuation"]["players"]), reverse=True)[:4]
    ) == pytest.approx(400)
    with pytest.raises(DataError, match="publication failed"):
        calculate_file(path, tmp_path / "one")
    assert one.read_bytes() == two.read_bytes()
    assert not list((tmp_path / "one").glob(".calculating-*"))


def test_calculation_rejects_tampering_and_missing_history_without_output(
    projection_bundle, tmp_path
):
    path = projection_bundle
    value = json.loads(path.read_text())
    value["players"][0]["history"] = []
    path.write_text(json.dumps(value))
    with pytest.raises(DataError, match="history"):
        calculate_file(path, tmp_path / "output")
    assert not (tmp_path / "output").exists()
    (path.parent / "config/model.json").write_text("{}")
    with pytest.raises(DataError, match="SHA-256"):
        load_calculation_input(path, CalibratedInput)


def test_calculation_settings_are_required_and_invalid_groups_fail_early(projection_bundle):
    path = projection_bundle.parent / "config/model.json"
    value = json.loads(path.read_text())
    for group in ("projection", "valuation"):
        for field in value[group]:
            altered = json.loads(json.dumps(value))
            del altered[group][field]
            with pytest.raises(ValidationError):
                PreparationModel.model_validate_json(json.dumps(altered))
    value["projection"]["rounding_groups"][0].append("AST")
    path.write_text(json.dumps(value))
    with pytest.raises(ConfigError, match="rounding_groups"):
        load_config(path.with_name("league.json"), path.with_name("season.json"), path)


def test_invalid_prior_is_rejected_without_publishing(projection_bundle, tmp_path):
    data = json.loads(projection_bundle.read_text())
    data["players"][0]["priors"][0]["stats"][0] = 100
    projection_bundle.write_text(json.dumps(data))
    with pytest.raises(DataError, match="nested count"):
        calculate_file(projection_bundle, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_result_directory_error_is_typed(projection_bundle, tmp_path):
    output = tmp_path / "file"
    output.write_text("preserve")
    with pytest.raises(DataError, match="publication failed"):
        calculate_file(projection_bundle, output)
    assert output.read_text() == "preserve"


def test_ragged_history_reports_data_error_through_cli(projection_bundle, tmp_path):
    data = json.loads(projection_bundle.read_text())
    data["players"][0]["history"].append([1.0])
    projection_bundle.write_text(json.dumps(data))
    output = tmp_path / "output"
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "fba.apps.cli",
            "calculate",
            str(projection_bundle),
            "--output",
            str(output),
        ],
        cwd=Path(__file__).parents[1] / "src",
        capture_output=True,
        text=True,
    )
    assert run.returncode == 2, run.stderr
    assert "projection.0" in json.loads(run.stderr)["error"]
    assert "history" in json.loads(run.stderr)["error"]
    with pytest.raises(DataError, match="history"):
        calculate_file(projection_bundle, output)
    assert not output.exists()


@pytest.mark.parametrize("offense", [[], ["FTM", "FTA"], ["FGM", "FGA", "FTM", "FTA", "TO"]])
def test_incomplete_offense_dependencies_fail_at_config_load(legacy_projection_bundle, offense):
    path = legacy_projection_bundle.parent / "config/model.json"
    data = json.loads(path.read_text())
    data["projection"]["offense_stats"] = offense
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="offense_stats"):
        load_config(path.with_name("league.json"), path.with_name("season.json"), path)


@pytest.fixture
def legacy_projection_bundle(projection_bundle):
    path = projection_bundle
    data = json.loads(path.read_text())
    config_dir = path.parent / "config"
    model = json.loads((Path(__file__).parent / "fixtures/resource-model-v2.json").read_text())
    model["valuation"].update(healthy_games=1, replacement_count=2)
    (config_dir / "model.json").write_text(json.dumps(model))
    config = load_config(*(config_dir / f"{name}.json" for name in ("league", "season", "model")))
    data.pop("calibration")
    data.pop("calibration_snapshot")
    data["format_version"] = 1
    data["config"] = config.model_dump(mode="json")
    for player in data["players"]:
        player.update(catalog=True, minutes_sd=1.0)
    for team in data["teams"]:
        team["possession_budget"] = 100.0
    data["artifacts"] = [
        {
            "path": str(p.relative_to(path.parent)),
            "sha256": digest(p.read_bytes()),
            "size": p.stat().st_size,
            "provenance": None,
        }
        for p in config_dir.glob("*.json")
    ]
    path.write_text(json.dumps(data))
    return path


def test_team_totals_do_not_cut_individual_minutes(projection_bundle, tmp_path):
    data = json.loads(projection_bundle.read_text())
    for player in data["players"]:
        player["team_id"] = "A"
        player["priors"][0].update(expected_games=4.0, minutes=30.0)
    projection_bundle.write_text(json.dumps(data))
    result = json.loads(calculate_file(projection_bundle, tmp_path / "output").read_text())
    assert all(p["minutes"] == 30.0 and p["expected_games"] == 2.0 for p in result["projections"])


def test_explicit_migration_preserves_sources_and_removes_sd_dependency(
    legacy_projection_bundle, tmp_path
):
    path = legacy_projection_bundle
    source_bytes = path.read_bytes()
    model = json.loads((Path(__file__).parents[1] / "examples/2026-27/model.json").read_text())
    model["valuation"].update(healthy_games=1, replacement_count=2)
    new_model = tmp_path / "new-model.json"
    new_model.write_text(json.dumps(model))
    with pytest.raises(DataError, match="format_version"):
        calculate_file(path, tmp_path / "unconverted")
    bundle = migrate_projection(
        path, new_model, next((tmp_path / "calibration").iterdir()), tmp_path / "converted"
    )
    converted = bundle / "projection-input.json"
    inputs, _ = load_calculation_input(converted, CalibratedInput)
    assert inputs.format_version == 3
    assert "minutes_sd" not in inputs.players[0].model_dump()
    assert (bundle / "migration/input-v1.json").read_bytes() == source_bytes
    assert path.read_bytes() == source_bytes
    first = json.loads(calculate_file(converted, tmp_path / "first").read_text())
    old = json.loads(source_bytes)
    for player in old["players"]:
        player["minutes_sd"] = 1000
    path.write_text(json.dumps(old))
    other = migrate_projection(
        path, new_model, next((tmp_path / "calibration").iterdir()), tmp_path / "changed-sd"
    )
    second = json.loads(
        calculate_file(other / "projection-input.json", tmp_path / "second").read_text()
    )
    assert first["projections"] == second["projections"]
    assert first["valuation"] == second["valuation"]
    with pytest.raises(DataError, match="already exists"):
        migrate_projection(
            path, new_model, next((tmp_path / "calibration").iterdir()), tmp_path / "changed-sd"
        )
    model["projection"]["stat_ids"].reverse()
    new_model.write_text(json.dumps(model))
    with pytest.raises(ConfigError, match="preserve the original axes"):
        migrate_projection(
            path, new_model, next((tmp_path / "calibration").iterdir()), tmp_path / "bad-axes"
        )
    assert not (tmp_path / "bad-axes").exists()


def test_migration_rejects_source_changed_after_verification(
    legacy_projection_bundle, tmp_path, monkeypatch
):
    from fba.adapters import migration

    original = migration.load_calculation_input

    def change_after_load(path, model):
        checked = original(path, model)
        artifact = path.parent / "config/model.json"
        artifact.write_bytes(artifact.read_bytes() + b"\n")
        return checked

    monkeypatch.setattr(migration, "load_calculation_input", change_after_load)
    model = Path(__file__).parents[1] / "examples/2026-27/model.json"
    output = tmp_path / "changed-during-read"
    with pytest.raises(DataError, match="changed during conversion"):
        migrate_projection(
            legacy_projection_bundle, model, next((tmp_path / "calibration").iterdir()), output
        )
    assert not output.exists()


def test_calibration_is_sourced_and_preserves_raw_replacement_eligibility(projection_bundle):
    from fba.core.calculation import calculate
    from fba.core.valuation import fit_ruler

    inputs, sha = load_calculation_input(projection_bundle, CalibratedInput)
    result = calculate(inputs, sha)
    # Raw GP=3 meets the configured threshold; calibrated GP=1.5 does not.
    assert inputs.config.model.valuation.healthy_games == 3
    assert all(p.expected_games == 1.5 for p in result.projections)
    with pytest.raises(DataError, match="healthy replacement"):
        fit_ruler(
            result.projections,
            (
                *inputs.config.model.projection.stat_ids,
                inputs.config.model.projection.threshold_stat,
            ),
            inputs.config.league,
            inputs.config.model.valuation,
        )
    for change in ({"training_season_id": "future"}, {"inputs_sha256": ()}, {"sample_size": 1}):
        broken = inputs.model_copy(
            update={"calibration": inputs.calibration.model_copy(update=change)}
        )
        with pytest.raises(DataError, match="sourced fit"):
            calculate(broken, sha)
    inputs = inputs.model_copy(update={"players": tuple(reversed(inputs.players))})
    assert calculate(inputs, sha) == result


def test_calibration_manifest_disagreement_is_rejected(projection_bundle, tmp_path):
    data = json.loads(projection_bundle.read_text())
    data["calibration"]["slope"] = 0.75
    projection_bundle.write_text(json.dumps(data))
    with pytest.raises(DataError, match="mismatched fit"):
        calculate_file(projection_bundle, tmp_path / "bad-fit")
    assert not (tmp_path / "bad-fit").exists()


@pytest.mark.parametrize("change", ["future", "season", "training"])
def test_migration_rejects_incompatible_calibration_snapshot(projection_bundle, tmp_path, change):
    from datetime import timedelta

    from fba.adapters.snapshots import load_snapshot

    data = json.loads(projection_bundle.read_text())
    root = tmp_path / data.pop("calibration_snapshot")
    data.pop("calibration")
    data["format_version"] = 2
    data["artifacts"] = [a for a in data["artifacts"] if not a["path"].startswith("calibration/")]
    projection_bundle.write_text(json.dumps(data))
    snapshot = load_snapshot(root)
    changes = {
        "future": {"as_of": snapshot.as_of + timedelta(days=1)},
        "season": {"season_id": "wrong"},
        "training": {
            "calibration": snapshot.calibration.model_copy(update={"training_season_id": "wrong"})
        },
    }
    candidate = snapshot.model_copy(update=changes[change])
    frozen = publish(
        candidate,
        {a.path: (root / a.path).read_bytes() for a in snapshot.artifacts},
        tmp_path / "new-calibration",
    )
    output = tmp_path / "bad-migration"
    with pytest.raises(DataError, match="wrong season or after snapshot cutoff"):
        migrate_projection(projection_bundle, tmp_path / "config/model.json", frozen, output)
    assert not output.exists()


def test_format_two_migration_retains_original_input(projection_bundle, tmp_path):
    data = json.loads(projection_bundle.read_text())
    root = tmp_path / data.pop("calibration_snapshot")
    data.pop("calibration")
    data["format_version"] = 2
    data["artifacts"] = [a for a in data["artifacts"] if not a["path"].startswith("calibration/")]
    projection_bundle.write_text(json.dumps(data))
    output = migrate_projection(
        projection_bundle, tmp_path / "config/model.json", root, tmp_path / "converted"
    )
    assert (output / "migration/input-v2.json").read_bytes() == projection_bundle.read_bytes()
    result = json.loads(
        calculate_file(output / "projection-input.json", tmp_path / "result").read_text()
    )
    assert all(p["expected_games"] == 1.5 for p in result["projections"])


def test_calibrated_games_cannot_exceed_return_schedule_or_manual_cap(projection_bundle):
    from fba.core.calculation import calculate

    inputs, sha = load_calculation_input(projection_bundle, CalibratedInput)
    inflated = inputs.calibration.model_copy(update={"intercept": 1.0, "slope": 1.0})
    for change, error in (
        ({"games_cap": 3.0}, "explicit cap"),
        (
            {
                "return_on": date(2026, 10, 24),
                "priors": (inputs.players[0].priors[0].model_copy(update={"expected_games": 1.0}),),
            },
            "eligible schedule",
        ),
    ):
        player = inputs.players[0].model_copy(update=change)
        altered = inputs.model_copy(
            update={"calibration": inflated, "players": (player, *inputs.players[1:])}
        )
        with pytest.raises(DataError, match=error):
            calculate(altered, sha)
