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
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import CalculationModel
from fba.contracts.projection import ProjectionInput


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
    files["model"]["valuation"]["healthy_games"] = 1
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
                "catalog": True,
                "priors": [
                    {"id": "provider", "expected_games": 3.0, "minutes": 15.0, "stats": stats}
                ],
                "games_cap": None,
                "return_on": None,
                "minutes_sd": 1.0,
                "history": [stats],
            }
        )
    dates = [str(date(2026, 10, 20) + timedelta(days=i * 2)) for i in range(3)]
    data = {
        "format_version": 1,
        "config": effective.model_dump(mode="json"),
        "players": players,
        "teams": [
            {"id": team, "dates": dates, "full_season_games": 4, "possession_budget": 100.0}
            for team in ("A", "B")
        ],
        "artifacts": [
            {
                "path": str(p.relative_to(tmp_path)),
                "sha256": digest(p.read_bytes()),
                "size": p.stat().st_size,
                "provenance": None,
            }
            for p in config_dir.glob("*.json")
        ],
    }
    path = tmp_path / "projection-input.json"
    path.write_bytes(canonical(ProjectionInput.model_validate_json(json.dumps(data))))
    return path


def test_calculation_rebuild_is_exact_and_does_not_overwrite(projection_bundle, tmp_path):
    path = projection_bundle
    one = calculate_file(path, tmp_path / "one")
    two = calculate_file(path, tmp_path / "two")
    assert one.read_bytes() == two.read_bytes()
    result = json.loads(one.read_text())
    assert result["input_sha256"] == digest(path.read_bytes())
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
        load_calculation_input(path, ProjectionInput)


def test_calculation_settings_are_required_and_invalid_groups_fail_early(projection_bundle):
    path = projection_bundle.parent / "config/model.json"
    value = json.loads(path.read_text())
    for group in ("projection", "valuation"):
        for field in value[group]:
            altered = json.loads(json.dumps(value))
            del altered[group][field]
            with pytest.raises(ValidationError):
                CalculationModel.model_validate_json(json.dumps(altered))
    value["projection"]["rounding_groups"][0].append("AST")
    path.write_text(json.dumps(value))
    with pytest.raises(ConfigError, match="rounding_groups"):
        load_config(path.with_name("league.json"), path.with_name("season.json"), path)


def test_invalid_reserve_prior_is_rejected_without_publishing(projection_bundle, tmp_path):
    data = json.loads(projection_bundle.read_text())
    data["players"][0]["catalog"] = False
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
def test_incomplete_offense_dependencies_fail_at_config_load(projection_bundle, offense):
    path = projection_bundle.parent / "config/model.json"
    data = json.loads(path.read_text())
    data["projection"]["offense_stats"] = offense
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="offense_stats"):
        load_config(path.with_name("league.json"), path.with_name("season.json"), path)
