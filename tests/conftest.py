import json

import pytest
from hypothesis import settings

from fba.contracts.config import ConfigBundle, LeagueRules, ModelConfig, SeasonConfig

settings.register_profile("deterministic", database=None, derandomize=True)
settings.load_profile("deterministic")


@pytest.fixture
def configs():
    evidence = {
        "kind": "Assumption",
        "reference": "test fixture",
        "as_of": "2024-09-01T00:00:00Z",
        "reason": "Synthetic test data",
    }
    league = {
        "format_version": 1,
        "league_id": "test",
        "teams": 12,
        "budget": 200,
        "minimum_bid": 1,
        "bid_increment": 1,
        "timezone": "America/New_York",
        "positions": ["PG", "C"],
        "starter_slots": [{"id": "one", "label": "one", "eligible_positions": ["PG", "C"]}],
        "bench_slots": 3,
        "injury_slots": [
            {"id": "injured", "label": "injured", "count": 2, "eligible_statuses": ["OUT"]}
        ],
        "categories": [
            {
                "id": "points",
                "label": "Points",
                "formula": {"kind": "linear", "terms": [{"stat_id": "PTS", "coefficient": 1.0}]},
                "direction": "higher",
                "comparison_decimals": 0,
                "tie_value": 0.5,
            }
        ],
        "scoring": {"mode": "h2h_one_win", "week_tie": "tie", "category_ties": "exclude"},
        "transactions": {
            "adds_per_period": 3,
            "add_periods": [{"id": "a", "start": "2025-10-20", "end": "2025-10-26"}],
            "effective": "same_day",
            "cutoff_local_time": "00:00",
            "waiver_days": 2,
        },
        "lineup": {"lock_mode": "daily", "lock_at": "player_game", "lock_local_time": "00:00"},
        "matchups": [{"id": "w", "start": "2025-10-20", "end": "2025-10-26", "phase": "playoff"}],
        "playoffs": {
            "team_count": 2,
            "week_ids": ["w"],
            "seeding": ["record", "seed"],
            "byes": 0,
            "reseed": False,
            "matchup_tie": "higher_seed",
        },
        "trade_deadline": None,
        "assumptions": [],
    }
    roles = [
        "projections",
        "game_logs",
        "schedule",
        "rosters",
        "official_schedule_counts",
        "historical_projections",
    ]
    season = {
        "format_version": 1,
        "season_id": "2025-26",
        "starts_on": "2025-10-20",
        "ends_on": "2025-10-26",
        "snapshot_as_of": "2025-10-01T00:00:00Z",
        "previous_season_id": "2024-25",
        "roster_import": {
            "path": "roster.csv",
            "format": "csv",
            "encoding": "utf-8",
            "delimiter": ",",
            "columns": {
                k: k
                for k in [
                    "player_id",
                    "name",
                    "positions",
                    "rank",
                    "projected_price",
                    "average_price",
                ]
            },
        },
        "identity_map_path": "identities.json",
        "manual_adjustments_path": "adjustments.json",
        "sources": [
            {
                "id": role,
                "role": role,
                "adapter": "test",
                "url": f"https://example.test/{role}",
                "season_code": "2026",
                "season_id": "2024-25"
                if role in ("game_logs", "historical_projections")
                else "2025-26",
                "available_as_of": "2025-09-01T00:00:00Z",
                "delivery": "manual",
                "manual_file": f"{role}.json",
                "manual_capture": {"sha256": "0" * 64, "retrieved_at": "2025-09-01T00:00:00Z"},
            }
            for role in roles
        ],
        "stat_definitions": [
            {"id": "PTS", "label": "Points", "unit": "count", "definition": {"kind": "observed"}}
        ],
        "assumptions": [],
    }
    model = {
        "format_version": 1,
        "calibration": {"value": "ordinary_least_squares", "evidence": evidence},
    }
    return league, season, model


@pytest.fixture
def parsed(configs):
    league, season, model = configs
    refs = ConfigBundle.model_validate_json(
        json.dumps(
            {
                k: {"schema_id": k, "input_sha256": "0" * 64, "effective_sha256": "1" * 64}
                for k in ("league", "season", "model")
            }
        )
    )
    return (
        LeagueRules.model_validate_json(json.dumps(league)),
        SeasonConfig.model_validate_json(json.dumps(season)),
        ModelConfig.model_validate_json(json.dumps(model)),
        refs,
    )
