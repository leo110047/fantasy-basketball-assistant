import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from fba.adapters.acquisition import acquire
from fba.adapters.codec import digest
from fba.adapters.snapshots import load_snapshot, publish
from fba.apps.cli import build, rebuild
from fba.contracts.base import ConfigError, DataError, IdentityError


def write(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def scenario(configs, tmp_path):
    league, season, model = configs
    season["snapshot_as_of"] = "2099-01-01T00:00:00Z"
    league_path, season_path, model_path = (
        tmp_path / f"{s}.json" for s in ("league", "season", "model")
    )
    stats = {
        "13": 4.0,
        "14": 8.0,
        "15": 2.0,
        "16": 3.0,
        "0": 11.0,
        "17": 1.0,
        "6": 4.0,
        "4": 1.0,
        "3": 2.0,
        "11": 1.0,
        "2": 1.0,
        "1": 0.0,
        "40": 20.0,
        "42": 1.0,
    }
    all_players = []
    for index in (1, 2):
        records = []
        for year, source, games in (
            (2026, 1, 70.0),
            (2025, 1, index * 10.0),
            (2025, 0, float(index)),
        ):
            records.append(
                {
                    "seasonId": year,
                    "statSourceId": source,
                    "statSplitTypeId": 0,
                    "externalId": str(year),
                    "proTeamId": index,
                    "stats": stats | {"42": games},
                }
            )
        for game in range(index):
            records.append(
                {
                    "seasonId": 2025,
                    "statSourceId": 0,
                    "statSplitTypeId": 5,
                    "externalId": f"old-{index}-{game}",
                    "proTeamId": index,
                    "stats": stats,
                }
            )
        all_players.append(
            {"id": index, "fullName": f"Player {index}", "proTeamId": index, "stats": records}
        )
    for source in season["sources"]:
        source["adapter"] = "espn_players"
        if source["role"] in ("game_logs", "historical_projections"):
            source["season_code"] = "2025"
        write(tmp_path / source["manual_file"], all_players)
    for role, adapter in (
        ("schedule", "espn_schedule"),
        ("rosters", "normalized_rosters"),
        ("official_schedule_counts", "official_counts"),
    ):
        next(s for s in season["sources"] if s["role"] == role)["adapter"] = adapter
    tipoff = int(datetime(2025, 10, 20, 23, tzinfo=UTC).timestamp() * 1000)
    game = {
        "id": 100,
        "date": tipoff,
        "homeProTeamId": 1,
        "awayProTeamId": 2,
        "postponed": False,
        "detail": "Scheduled",
    }
    write(
        tmp_path / "schedule.json",
        {
            "seasonId": 2026,
            "settings": {
                "proTeams": [
                    {"id": 1, "abbrev": "A", "proGamesByScoringPeriod": {"1": [game]}},
                    {"id": 2, "abbrev": "B", "proGamesByScoringPeriod": {"1": [game]}},
                ]
            },
        },
    )
    write(
        tmp_path / "rosters.json",
        {
            "format_version": 1,
            "season_id": "2025-26",
            "players": [
                {"provider": "espn", "id": str(i), "name": f"Player {i}", "team_id": str(i)}
                for i in (1, 2)
            ],
        },
    )
    write(
        tmp_path / "official_schedule_counts.json",
        {
            "format_version": 1,
            "season_id": "2025-26",
            "counts": [
                {
                    "team_id": str(i),
                    "announced": 1,
                    "pending": 2,
                    "pending_reason": "Cup",
                    "source_id": "official_schedule_counts",
                }
                for i in (1, 2)
            ],
        },
    )
    (tmp_path / "roster.csv").write_text(
        "player_id,name,positions,rank,projected_price,average_price\n"
        "a,Player 1,PG,1,10,11\nb,Player 2,C,2,,-\n"
    )
    write(
        tmp_path / "identities.json",
        {
            "format_version": 1,
            "entries": [
                {
                    "player_id": k,
                    "provider": "espn",
                    "provider_player_id": str(i),
                    "method": "exported_id",
                    "source": "Synthetic fixture",
                    "confirmed_at": "2025-09-01T00:00:00Z",
                }
                for k, i in (("a", 1), ("b", 2))
            ],
        },
    )
    write(tmp_path / "adjustments.json", {"format_version": 1, "adjustments": []})
    for path, value in ((league_path, league), (season_path, season), (model_path, model)):
        for source in season["sources"]:
            source["manual_capture"]["sha256"] = digest(
                (tmp_path / source["manual_file"]).read_bytes()
            )
        write(path, value)
    return league_path, season_path, model_path


def test_build_and_offline_rebuild_all_bytes_identical(scenario, tmp_path):
    original = build(*scenario, tmp_path / "first", 1)
    expected = {p.relative_to(original): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    with patch("fba.adapters.acquisition.urlopen", side_effect=AssertionError("network forbidden")):
        restored = rebuild(original, tmp_path / "second")
    actual = {p.relative_to(restored): p.read_bytes() for p in restored.rglob("*") if p.is_file()}
    assert expected == actual
    snapshot = load_snapshot(restored)
    assert snapshot.calibration.intercept == 0.0
    assert snapshot.calibration.slope == 0.1
    assert snapshot.players[1].roster.average_price is None


def test_roster_pages_build_and_rebuild_without_api_fallback(scenario, tmp_path):
    season = json.loads(scenario[1].read_bytes())
    previous = next(s for s in season["sources"] if s["role"] == "rosters")
    season["sources"].remove(previous)
    for team in ("1", "2"):
        name = f"roster-{team}.html"
        payload = {
            "page": {
                "content": {
                    "roster": {
                        "metadata": {"year": 2026},
                        "team": {"id": team},
                        "athletes": [{"id": team, "name": f"Player {team}"}],
                    }
                }
            }
        }
        data = ("<script>window['__espnfitt__']=" + json.dumps(payload) + ";</script>").encode()
        (tmp_path / name).write_bytes(data)
        season["sources"].append(
            previous
            | {
                "id": f"roster-{team}",
                "adapter": "espn_roster_page",
                "manual_file": name,
                "manual_capture": previous["manual_capture"] | {"sha256": digest(data)},
            }
        )
    write(scenario[1], season)
    original = build(*scenario, tmp_path / "first", 1)
    with patch("fba.adapters.acquisition.urlopen", side_effect=AssertionError("network forbidden")):
        restored = rebuild(original, tmp_path / "second")
    assert (restored / "snapshot.json").read_bytes() == (original / "snapshot.json").read_bytes()
    assert [p.team_id for p in load_snapshot(restored).players] == ["1", "2"]


def test_missing_identity_produces_no_partial_snapshot(scenario, tmp_path):
    write(tmp_path / "identities.json", {"format_version": 1, "entries": []})
    with pytest.raises(IdentityError) as error:
        build(*scenario, tmp_path / "output", 1)
    assert error.value.unresolved == ("a", "b")
    assert not (tmp_path / "output").exists()


def test_bad_data_produces_no_partial_snapshot(scenario, tmp_path):
    path = tmp_path / "official_schedule_counts.json"
    data = json.loads(path.read_bytes())
    data["counts"][0]["announced"] = 2
    write(path, data)
    season = json.loads(scenario[1].read_bytes())
    next(s for s in season["sources"] if s["id"] == "official_schedule_counts")["manual_capture"][
        "sha256"
    ] = digest(path.read_bytes())
    write(scenario[1], season)
    with pytest.raises(DataError, match="count mismatch"):
        build(*scenario, tmp_path / "output", 1)
    assert not (tmp_path / "output").exists()


def test_snapshot_tampering_is_rejected(scenario, tmp_path):
    root = build(*scenario, tmp_path / "output", 1)
    (root / "inputs/roster.csv").write_bytes(b"tampered")
    with pytest.raises(DataError, match="SHA-256"):
        load_snapshot(root)


def test_publication_failure_removes_staging_tree(scenario, tmp_path):
    root = build(*scenario, tmp_path / "original", 1)
    snapshot = load_snapshot(root)
    files = {a.path: (root / a.path).read_bytes() for a in snapshot.artifacts}
    with patch.object(Path, "rename", side_effect=OSError("disk full")):
        with pytest.raises(DataError, match="publication failed"):
            publish(snapshot, files, tmp_path / "failed")
    assert list((tmp_path / "failed").iterdir()) == []


def test_same_snapshot_cannot_be_overwritten(scenario, tmp_path):
    root = build(*scenario, tmp_path / "output", 1)
    before = digest((root / "snapshot.json").read_bytes())
    with pytest.raises(DataError, match="already exists"):
        rebuild(root, tmp_path / "output")
    assert digest((root / "snapshot.json").read_bytes()) == before


def test_changed_config_changes_snapshot_identity(scenario, tmp_path):
    before = load_snapshot(build(*scenario, tmp_path / "first", 1))
    league = json.loads(scenario[0].read_bytes())
    league["teams"] = 16
    write(scenario[0], league)
    after = load_snapshot(build(*scenario, tmp_path / "second", 1))
    assert before.config.league != after.config.league


def test_future_identity_rejected(scenario, tmp_path):
    path = tmp_path / "identities.json"
    data = json.loads(path.read_bytes())
    data["entries"][0]["confirmed_at"] = "2100-01-01T00:00:00Z"
    write(path, data)
    with pytest.raises(DataError, match="after snapshot"):
        build(*scenario, tmp_path / "output", 1)


def test_mixed_provider_seasons_rejected_before_calibration(scenario, tmp_path):
    season = json.loads(scenario[1].read_bytes())
    training = next(s for s in season["sources"] if s["role"] == "historical_projections")
    training["season_code"] = "2024"
    path = tmp_path / training["manual_file"]
    players = json.loads(path.read_bytes())
    for player in players:
        for record in player["stats"]:
            if record["seasonId"] == 2025 and record["statSourceId"] == 1:
                record["seasonId"] = 2024
    write(path, players)
    training["manual_capture"]["sha256"] = digest(path.read_bytes())
    write(scenario[1], season)
    with pytest.raises((ConfigError, DataError), match="season_code"):
        build(*scenario, tmp_path / "output", 1)
    assert not (tmp_path / "output").exists()


def test_config_edit_during_acquisition_prevents_publication(scenario, tmp_path):
    def change_then_acquire(source, base, cutoff):
        league = json.loads(scenario[0].read_bytes())
        league["teams"] = 16
        write(scenario[0], league)
        return acquire(source, base, cutoff)

    with patch("fba.apps.cli.acquire", side_effect=change_then_acquire):
        with pytest.raises(ConfigError, match="changed during acquisition"):
            build(*scenario, tmp_path / "output", 1)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("teams", [12, 16])
def test_alternative_league_rules_reach_snapshot(scenario, tmp_path, teams):
    league = json.loads(scenario[0].read_bytes())
    season = json.loads(scenario[1].read_bytes())
    league["teams"] = teams
    league["lineup"] = {"lock_mode": "weekly", "lock_at": "first_game", "lock_local_time": "00:00"}
    league["starter_slots"].append(
        {"id": "util", "label": "UTIL", "eligible_positions": league["positions"]}
    )
    league["bench_slots"] = 3
    league["transactions"]["adds_per_period"] = 3
    stats = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "TO", "FGM", "FGA", "FTM", "FTA"]
    season["stat_definitions"] = [
        {"id": s, "label": s, "unit": "count", "definition": {"kind": "observed"}} for s in stats
    ]
    categories = []
    for stat in stats[:7]:
        categories.append(
            {
                "id": stat,
                "label": stat,
                "formula": {"kind": "linear", "terms": [{"stat_id": stat, "coefficient": 1.0}]},
                "direction": "lower" if stat == "TO" else "higher",
                "comparison_decimals": 0,
                "tie_value": 0.5,
            }
        )
    for name, numerator, denominator in (("FG%", "FGM", "FGA"), ("FT%", "FTM", "FTA")):
        categories.append(
            {
                "id": name,
                "label": name,
                "formula": {
                    "kind": "ratio",
                    "numerator": [{"stat_id": numerator, "coefficient": 1.0}],
                    "denominator": [{"stat_id": denominator, "coefficient": 1.0}],
                    "zero_denominator": "zero",
                },
                "direction": "higher",
                "comparison_decimals": 4,
                "tie_value": 0.5,
            }
        )
    league["categories"] = categories
    write(scenario[0], league)
    write(scenario[1], season)
    snapshot = load_snapshot(build(*scenario, tmp_path / "output", 1))
    assert len(snapshot.players) == 2
    assert snapshot.config.league.input_sha256 == digest(scenario[0].read_bytes())
