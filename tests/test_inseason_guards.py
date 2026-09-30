import json
import subprocess
import sys
from datetime import timedelta

import pytest
from inseason_support import fixture, parameters
from test_inseason_backtest import study
from test_inseason_service import selected_session

from fba.contracts.base import DataError
from fba.contracts.inseason import SyncState
from fba.data.codec import canonical, digest
from fba.inseason.backtest import run_study
from fba.inseason.operations import calibration_status, import_validation


@pytest.mark.parametrize(
    "failed",
    ["projection_passed", "calibration_passed", "availability_passed", "production_passed"],
)
def test_failed_holdout_cannot_import_or_unlock(tmp_path, failed):
    session = selected_session(tmp_path / "session")
    params, data = parameters(), study()
    report = run_study(data, params, digest(canonical(data)), digest(canonical(params)))
    report = report.model_copy(
        update={
            "projection_passed": True,
            "calibration_passed": True,
            "availability_passed": True,
            "production_passed": True,
            failed: False,
        }
    )
    path = tmp_path / "report.json"
    path.write_bytes(canonical(report))
    original = (session.store.root / "parameters.json").read_bytes()
    with pytest.raises(DataError, match="holdout gate"):
        import_validation(session, path)
    assert (session.store.root / "parameters.json").read_bytes() == original
    assert not (session.league_store().root / "validation.json").exists()
    session.params = report.fitted_parameters
    session.league_store().write("validation.json", report)
    assert calibration_status(session)["enabled"] is False
    session.league_store().write("validation.json", report.model_copy(update={failed: True}))
    assert calibration_status(session)["enabled"] is True


def test_fresh_yahoo_cannot_mask_expired_player_source(tmp_path):
    session = selected_session(tmp_path)
    _, _, players, priors, _, snapshot, at = fixture()
    now = at + timedelta(days=3)
    store = session.league_store()
    state = session.state().model_copy(
        update={
            "players_sha256": store.snapshot(
                "players", players.as_of, players.model_dump(mode="json")
            ),
            "priors_sha256": store.snapshot("priors", at, priors.model_dump(mode="json")),
            "normalized_sha256": store.snapshot(
                "Yahoo", now, snapshot.model_copy(update={"as_of": now}).model_dump(mode="json")
            ),
            "sync": SyncState(
                connected=True,
                authorization_valid=True,
                last_success=now,
                last_error="NBA feed unavailable",
                settings_pending=False,
                unresolved_rostered=(),
            ),
        }
    )
    session.save_state(state)
    with pytest.raises(DataError, match="NBA"):
        session.simulation(as_of=now)
    fresh = players.model_copy(update={"as_of": now})
    session.save_state(
        state.model_copy(
            update={"players_sha256": store.snapshot("players", now, fresh.model_dump(mode="json"))}
        )
    )
    assert session.simulation(as_of=now).players.as_of == now


@pytest.mark.parametrize("command", ["inseason-backtest", "inseason-replay", "inseason-calibrate"])
def test_module_study_commands_reach_input_validation(tmp_path, command):
    args = [
        sys.executable,
        "-m",
        "fba.apps.cli",
        command,
        str(tmp_path / "missing.json"),
        "--output",
        str(tmp_path / "unused.json"),
    ]
    if command != "inseason-calibrate":
        args.extend(["--parameters", str(tmp_path / "parameters.json")])
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "NameError" not in result.stderr
    assert str(tmp_path / "missing.json") in json.loads(result.stderr)["error"]
    assert not (tmp_path / "unused.json").exists()
