import copy
import io
import json
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from pydantic import ValidationError

from fba.adapters.acquisition import acquire
from fba.adapters.codec import digest
from fba.adapters.config import load_config
from fba.adapters.espn import schedule
from fba.adapters.snapshots import checked_path
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import LeagueRules, ModelConfig, SeasonConfig, Source


def paths(value, prefix=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield prefix + (key,)
            yield from paths(child, prefix + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from paths(child, prefix + (index,))


@pytest.mark.parametrize("index,model", [(0, LeagueRules), (1, SeasonConfig), (2, ModelConfig)])
def test_every_supplied_configuration_field_is_required(configs, index, model):
    for path in paths(configs[index]):
        altered = copy.deepcopy(configs[index])
        parent = altered
        for key in path[:-1]:
            parent = parent[key]
        del parent[path[-1]]
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(altered))


def test_format_version_cannot_be_boolean(configs):
    league = configs[0] | {"format_version": True}
    with pytest.raises(ValidationError, match="format_version"):
        LeagueRules.model_validate_json(json.dumps(league))


def test_timezone_resolves_at_file_boundary(configs, tmp_path):
    configs[0]["timezone"] = "Invalid/Zone"
    files = tuple(tmp_path / f"{i}.json" for i in range(3))
    for path, data in zip(files, configs, strict=True):
        path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="timezone"):
        load_config(*files)


@pytest.mark.parametrize("unsafe", ["../escape", "/absolute", "sub/../../escape", "a\\b"])
def test_snapshot_paths_do_not_escape(tmp_path, unsafe):
    with pytest.raises(DataError, match="unsafe"):
        checked_path(tmp_path, unsafe)


def test_snapshot_symlink_cannot_escape(tmp_path):
    (tmp_path / "outside").symlink_to(tmp_path.parent)
    with pytest.raises(DataError, match="escapes"):
        checked_path(tmp_path, "outside/secret")


def source(configs, **changes):
    raw = configs[1]["sources"][0] | changes
    return Source.model_validate_json(json.dumps(raw))


def test_manual_bytes_must_match_capture(configs, tmp_path):
    (tmp_path / "projections.json").write_bytes(b"tampered")
    with pytest.raises(DataError, match="SHA-256"):
        acquire(source(configs), tmp_path, datetime(2099, 1, 1, tzinfo=UTC))


def test_manual_capture_keeps_original_retrieval_time(configs, tmp_path):
    data = b"original"
    (tmp_path / "projections.json").write_bytes(data)
    captured = "2025-09-01T00:00:00Z"
    result = acquire(
        source(configs, manual_capture={"sha256": digest(data), "retrieved_at": captured}),
        tmp_path,
        datetime(2025, 10, 1, tzinfo=UTC),
    )
    assert result.provenance.retrieved_at == datetime(2025, 9, 1, tzinfo=UTC)


def test_network_failure_never_uses_manual_file(configs, tmp_path):
    spec = source(configs, delivery="fetch", manual_file=None, manual_capture=None)
    (tmp_path / "projections.json").write_text("stale")
    with patch(
        "fba.adapters.acquisition.urlopen",
        side_effect=HTTPError(spec.url, 403, "Forbidden", {}, None),
    ):
        with pytest.raises(DataError, match="403"):
            acquire(spec, tmp_path, datetime(2099, 1, 1, tzinfo=UTC))


def test_espn_fetch_requests_full_page(configs, tmp_path):
    spec = source(
        configs, delivery="fetch", adapter="espn_players", manual_file=None, manual_capture=None
    )
    response = io.BytesIO(b'[{"id":1}]')
    response.url = spec.url
    with patch("fba.adapters.acquisition.urlopen", return_value=response) as fetch:
        acquire(spec, tmp_path, datetime(2099, 1, 1, tzinfo=UTC))
    request = fetch.call_args.args[0]
    filtering = json.loads(request.get_header("X-fantasy-filter"))["players"]
    assert filtering["limit"] == 10000
    assert filtering["sortPercOwned"] == {"sortPriority": 1, "sortAsc": False}


def test_wrong_schedule_year_is_rejected():
    data = {
        "settings": {
            "proTeams": [
                {
                    "id": 1,
                    "abbrev": "A",
                    "proGamesByScoringPeriod": {
                        "1": [
                            {
                                "id": 1,
                                "date": 0,
                                "homeProTeamId": 1,
                                "awayProTeamId": 2,
                                "postponed": False,
                                "detail": "Scheduled",
                            }
                        ]
                    },
                }
            ]
        }
    }
    with pytest.raises(DataError, match="outside requested season"):
        schedule(json.dumps(data).encode(), 2026, "test", "UTC")


@pytest.mark.parametrize("wrapped", [False, True])
def test_espn_full_response_limit_cannot_be_frozen(configs, tmp_path, wrapped):
    spec = source(
        configs, delivery="fetch", adapter="espn_players", manual_file=None, manual_capture=None
    )
    rows = [{"id": i} for i in range(10000)]
    response = io.BytesIO(json.dumps({"players": rows} if wrapped else rows).encode())
    response.url = spec.url
    with patch("fba.adapters.acquisition.urlopen", return_value=response):
        with pytest.raises(DataError, match="pagination limit"):
            acquire(spec, tmp_path, datetime(2099, 1, 1, tzinfo=UTC))


def test_espn_wrapped_fetch_response_is_preserved(configs, tmp_path):
    spec = source(
        configs, delivery="fetch", adapter="espn_players", manual_file=None, manual_capture=None
    )
    data = b'{"players":[{"player":{"id":1}}]}'
    response = io.BytesIO(data)
    response.url = spec.url
    with patch("fba.adapters.acquisition.urlopen", return_value=response):
        result = acquire(spec, tmp_path, datetime(2099, 1, 1, tzinfo=UTC))
    assert result.data == data
    assert result.provenance.raw_sha256 == digest(data)
