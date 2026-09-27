import json

import pytest
from pydantic import ValidationError

from fba.adapters.codec import checked_json
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import LeagueRules
from fba.core.config import validate_config


def test_valid_config_and_immutable(parsed):
    config = validate_config(*parsed)
    with pytest.raises(ValidationError):
        config.league.teams = 16
    assert config.league.transactions.adds_per_period == 3


@pytest.mark.parametrize("field,value", [("teams", "12"), ("budget", True), ("minimum_bid", 0)])
def test_reject_coercion_and_invalid_values(configs, field, value):
    league, _, _ = configs
    league[field] = value
    with pytest.raises(ValidationError, match=field):
        LeagueRules.model_validate_json(json.dumps(league))


def test_missing_field_has_no_default(configs):
    league, _, _ = configs
    del league["bench_slots"]
    with pytest.raises(ValidationError, match="bench_slots"):
        LeagueRules.model_validate_json(json.dumps(league))


@pytest.mark.parametrize(
    "change,path",
    [
        ({"budget": 1}, "budget"),
        ({"positions": ["PG"]}, "starter_slots"),
    ],
)
def test_cross_field_validation(configs, parsed, change, path):
    league = configs[0] | change
    with pytest.raises(ConfigError, match=path):
        validate_config(LeagueRules.model_validate_json(json.dumps(league)), *parsed[1:])


def test_category_unknown_stat(configs, parsed):
    league = configs[0]
    league["categories"][0]["formula"]["terms"][0]["stat_id"] = "missing"
    with pytest.raises(ConfigError, match="categories.points.formula"):
        validate_config(LeagueRules.model_validate_json(json.dumps(league)), *parsed[1:])


def test_weekly_player_lock_conflict(configs, parsed):
    league = configs[0]
    league["lineup"]["lock_mode"] = "weekly"
    with pytest.raises(ConfigError, match="lock_at"):
        validate_config(LeagueRules.model_validate_json(json.dumps(league)), *parsed[1:])


def test_duplicate_json_keys_fail():
    with pytest.raises(DataError, match="duplicate key"):
        checked_json(b'{"teams":12,"teams":16}', "league.json")
