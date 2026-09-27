import json
from datetime import UTC, datetime, timedelta

import pytest
from test_calculation import projection_bundle as projection_bundle

from fba.adapters.calculation import calculate_file, load_calculation_input
from fba.adapters.codec import canonical
from fba.adapters.config import load_config
from fba.adapters.preparation import project
from fba.adapters.snapshots import artifact, load_snapshot, publish
from fba.contracts.base import ConfigError, DataError
from fba.contracts.data import (
    Adjustment,
    ExpectedGames,
    Forecast,
    Game,
    ManualAdjustments,
    Multiply,
    Player,
    PlayerGame,
    ReturnAt,
    RosterRow,
    ScheduleCount,
    StatValue,
)
from fba.contracts.projection import CalibratedInput, PreparedInput
from fba.core.preparation import prepare


@pytest.fixture
def annual_case(projection_bundle, tmp_path):
    inputs, _ = load_calculation_input(projection_bundle, CalibratedInput)
    model_path = tmp_path / "config/model.json"
    model = json.loads(model_path.read_text())
    model["preparation"].update(
        minimum_player_history=2,
        donor_minimum_history=2,
        donor_minutes_lower=0.0,
        donor_minutes_upper=100.0,
        historical_games_lower=0.0,
        historical_games_upper=4.0,
    )
    model_path.write_text(json.dumps(model))
    config = load_config(*(tmp_path / f"config/{n}.json" for n in ("league", "season", "model")))
    snapshot = load_snapshot(tmp_path / inputs.calibration_snapshot)
    axes = config.model.projection.stat_ids
    players, forecasts, history = [], [], []
    for i, p in enumerate(inputs.players):
        players.append(
            Player(
                roster=RosterRow(
                    id=p.id,
                    name=p.name,
                    positions=("PG",),
                    rank=i + 1,
                    projected_price=None,
                    average_price=None,
                ),
                identities=(),
                team_id=p.team_id,
                history_status="available" if i else "no_previous_season_history",
            )
        )
        values = (*p.priors[0].stats, p.priors[0].minutes)
        forecasts.append(
            Forecast(
                player_id=p.id,
                expected_games=3.0,
                source_id="forecast",
                totals=tuple(
                    StatValue(
                        id=s,
                        value=None if s in ("OREB", "3PM") else v * 3,
                        missing_reason="source absent" if s in ("OREB", "3PM") else None,
                    )
                    for s, v in zip((*axes, "MIN"), values, strict=True)
                ),
            )
        )
        if i:
            for g in range(3):
                history.append(
                    PlayerGame(
                        player_id=p.id,
                        game_id=str(g),
                        team_id=p.team_id,
                        source_id="history",
                        stats=tuple(
                            StatValue(id=s, value=v, missing_reason=None)
                            for s, v in zip((*axes, "MIN"), values, strict=True)
                        ),
                    )
                )
    games = tuple(
        Game(
            id=str(i),
            home_team_id="A",
            away_team_id="B",
            tipoff=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            local_date=day,
            source_id="schedule",
            status="scheduled",
        )
        for i, day in enumerate(inputs.teams[0].dates)
    )
    files = {
        a.path: (tmp_path / inputs.calibration_snapshot / a.path).read_bytes()
        for a in snapshot.artifacts
    }
    files.update(
        {
            f"config/{n}.json": (tmp_path / f"config/{n}.json").read_bytes()
            for n in ("league", "season", "model")
        }
    )
    files["config/effective.json"] = canonical(config)
    candidate = snapshot.model_copy(
        update={
            "config": config.refs,
            "players": tuple(players),
            "forecasts": tuple(forecasts),
            "history": tuple(history),
            "schedule": games,
            "schedule_counts": tuple(
                ScheduleCount(
                    team_id=t,
                    announced=3,
                    pending=1,
                    pending_reason="pending",
                    source_id="schedule",
                )
                for t in ("A", "B")
            ),
            "artifacts": tuple(artifact(p, d) for p, d in sorted(files.items())),
        }
    )
    frozen = publish(candidate, files, tmp_path / "annual-source")
    return frozen, model_path, config, candidate


def test_snapshot_to_valuation_rebuild_and_previous_diff_are_exact(annual_case, tmp_path):
    snapshot, model, _, _ = annual_case
    one = project(snapshot, model, tmp_path / "one", None)
    two = project(snapshot, model, tmp_path / "two", one)
    assert (one / "projection-input.json").read_bytes() == (
        two / "projection-input.json"
    ).read_bytes()
    result = next((one / "results").glob("calculation-*.json"))
    rebuilt = calculate_file(one / "projection-input.json", tmp_path / "rebuilt")
    assert result.read_bytes() == rebuilt.read_bytes()
    diff = json.loads((two / "difference.json").read_text())
    assert all(p["delta"] == 0 for p in diff["largest_changes"])
    inputs, _ = load_calculation_input(one / "projection-input.json", PreparedInput)
    assert len(inputs.history_pools) == 1
    assert next(p for p in inputs.players if p.id == "0").history_pool_id == "guard"
    assert not next(p for p in inputs.players if p.id == "0").history
    assert any(n.kind == "Assumption" and "OREB" in n.detail for n in inputs.notes)
    with pytest.raises(DataError, match="already exists"):
        project(snapshot, model, tmp_path / "one", None)
    assert not list((tmp_path / "one").glob(".projecting-*"))


def test_preparation_uses_exact_identity_and_history_share(annual_case):
    _, _, config, snapshot = annual_case
    population = prepare(snapshot, config)
    player = next(p for p in population.players if p.id == "1")
    forecast = next(p for p in player.priors if p.id == "b")
    original = next(g for g in snapshot.history if g.player_id == "1")
    stats = {s.id: s.value for s in original.stats}
    axes = config.model.projection.stat_ids
    assert forecast.stats[axes.index("3PM")] == pytest.approx(stats["3PM"])
    assert forecast.stats[axes.index("OREB")] == pytest.approx(stats["OREB"])
    shuffled = snapshot.model_copy(
        update={
            "players": tuple(reversed(snapshot.players)),
            "history": tuple(reversed(snapshot.history)),
            "forecasts": tuple(reversed(snapshot.forecasts)),
        }
    )
    assert prepare(shuffled, config) == population


def test_missing_data_is_explicit_and_incomplete_veteran_is_not_rookie(annual_case):
    _, _, config, snapshot = annual_case
    players = list(snapshot.players)
    players[0] = players[0].model_copy(update={"history_status": "incomplete"})
    candidate = snapshot.model_copy(update={"players": tuple(players)})
    prepared = prepare(candidate, config)
    assert any("status=incomplete" in n.detail for n in prepared.notes if n.player_id == "0")
    candidate = candidate.model_copy(
        update={"forecasts": tuple(f for f in candidate.forecasts if f.player_id != "0")}
    )
    prepared = prepare(candidate, config)
    assert not next(p for p in prepared.players if p.id == "0").priors
    assert any(n.kind == "unavailable" for n in prepared.notes if n.player_id == "0")
    empty = snapshot.model_copy(update={"history": ()})
    with pytest.raises(DataError, match="denominator is zero"):
        prepare(empty, config)


def adjustment(snapshot, pid, operation, identity):
    return Adjustment(
        id=identity,
        player_id=pid,
        published_at=snapshot.as_of,
        effective_from=snapshot.as_of,
        reason="test",
        source="fixture",
        assumption=True,
        operation=operation,
    )


def test_manual_operations_are_ordered_and_gp_overrides_after_calibration(annual_case, tmp_path):
    root, model, config, snapshot = annual_case
    changes = (
        adjustment(snapshot, "0", Multiply(kind="multiply", stat_id="AST", factor=1.1), "a"),
        adjustment(snapshot, "0", ExpectedGames(kind="expected_games", games=2.0), "b"),
        adjustment(
            snapshot,
            "0",
            ReturnAt(kind="return_at", return_at=datetime(2026, 10, 22, tzinfo=UTC)),
            "c",
        ),
    )
    modified = snapshot.model_copy(
        update={"adjustments": ManualAdjustments(format_version=1, adjustments=changes)}
    )
    prepared = prepare(modified, config)
    player = next(p for p in prepared.players if p.id == "0")
    assert player.expected_games_override == 2.0
    assert player.games_cap == 2.0
    files = {a.path: (root / a.path).read_bytes() for a in modified.artifacts}
    frozen = publish(modified, files, tmp_path / "adjusted")
    bundle = project(frozen, model, tmp_path / "projection", None)
    result = json.loads(next((bundle / "results").glob("*.json")).read_text())
    row = next(p for p in result["projections"] if p["id"] == "0")
    assert row["expected_games"] == 2.0
    assert row["stats"][config.model.projection.stat_ids.index("AST")] == pytest.approx(2.2)


def test_failure_publishes_no_partial_projection(annual_case, tmp_path):
    root, model, config, snapshot = annual_case
    change = adjustment(snapshot, "0", ExpectedGames(kind="expected_games", games=100.0), "bad")
    candidate = snapshot.model_copy(
        update={"adjustments": ManualAdjustments(format_version=1, adjustments=(change,))}
    )
    frozen = publish(
        candidate,
        {a.path: (root / a.path).read_bytes() for a in snapshot.artifacts},
        tmp_path / "bad-snapshot",
    )
    with pytest.raises(DataError, match="eligible schedule"):
        project(frozen, model, tmp_path / "output", None)
    assert not (tmp_path / "output").exists()
    broken = json.loads(model.read_text())
    broken["preparation"]["position_pools"][0]["any_positions"] = ["unknown"]
    model.write_text(json.dumps(broken))
    with pytest.raises(ConfigError, match="position_pools"):
        load_config(root / "config/league.json", root / "config/season.json", model)


def test_future_and_midseason_adjustments_are_rejected(annual_case):
    _, _, config, snapshot = annual_case
    operation = Multiply(kind="multiply", stat_id="AST", factor=1.1)
    for change, error in (
        ({"published_at": snapshot.as_of + timedelta(days=1)}, "future publication"),
        ({"effective_from": datetime(2027, 1, 1, tzinfo=UTC)}, "midseason"),
    ):
        record = adjustment(snapshot, "0", operation, "bad").model_copy(update=change)
        altered = snapshot.model_copy(
            update={"adjustments": ManualAdjustments(format_version=1, adjustments=(record,))}
        )
        with pytest.raises(DataError, match=error):
            prepare(altered, config)


def test_reported_zero_minute_rows_are_excluded_and_original_is_preserved(annual_case):
    _, _, config, snapshot = annual_case
    original = prepare(snapshot, config)
    zero = snapshot.history[0].model_copy(
        update={
            "game_id": "did-not-play",
            "stats": tuple(s.model_copy(update={"value": 0.0}) for s in snapshot.history[0].stats),
        }
    )
    changed = snapshot.model_copy(update={"history": (*snapshot.history, zero)})
    actual = prepare(changed, config)
    assert actual.players == original.players
    assert actual.history_pools == original.history_pools
    assert any("reported zero-minute" in n.detail for n in actual.notes)
    broken = zero.model_copy(
        update={
            "stats": tuple(
                s.model_copy(update={"value": 1.0}) if s.id == "PTS" else s for s in zero.stats
            )
        }
    )
    # ESPN can report zero whole minutes with a nonzero statistic. The reference
    # filters on the reported minutes, without reclassifying those rows as DNP.
    filtered = prepare(snapshot.model_copy(update={"history": (*snapshot.history, broken)}), config)
    assert filtered.players == original.players
    assert any(s.value == 1.0 for s in broken.stats)
    assert any("reported zero-minute" in n.detail for n in filtered.notes)


def test_unknown_or_duplicate_manual_adjustments_cannot_be_ignored(annual_case):
    _, _, config, snapshot = annual_case
    item = adjustment(snapshot, "0", ExpectedGames(kind="expected_games", games=2.0), "a")
    for records, error in (
        ((item, item), "duplicate IDs"),
        ((item.model_copy(update={"player_id": "unknown"}),), "unknown player"),
    ):
        candidate = snapshot.model_copy(
            update={"adjustments": ManualAdjustments(format_version=1, adjustments=records)}
        )
        with pytest.raises(DataError, match=error):
            prepare(candidate, config)


def test_previous_result_tampering_leaves_no_partial_bundle(annual_case, tmp_path):
    root, model, _, _ = annual_case
    old = project(root, model, tmp_path / "old", None)
    result = next((old / "results").glob("*.json"))
    result.write_bytes(result.read_bytes() + b" ")
    with pytest.raises(DataError, match="result hash mismatch"):
        project(root, model, tmp_path / "output", old)
    assert not list((tmp_path / "output").iterdir())


def test_zero_gp_without_history_or_pool_does_not_abort_other_players(annual_case, tmp_path):
    root, model, _, snapshot = annual_case
    players = tuple(
        p.model_copy(update={"roster": p.roster.model_copy(update={"positions": ("C",)})})
        if p.roster.id == "0"
        else p
        for p in snapshot.players
    )
    forecasts = tuple(
        f.model_copy(
            update={
                "expected_games": 0.0,
                "totals": tuple(
                    s.model_copy(
                        update={
                            "value": None if s.id == "OREB" else 0.0,
                            "missing_reason": "source absent" if s.id == "OREB" else None,
                        }
                    )
                    for s in f.totals
                ),
            }
        )
        if f.player_id == "0"
        else f
        for f in snapshot.forecasts
    )
    candidate = snapshot.model_copy(update={"players": players, "forecasts": forecasts})
    frozen = publish(
        candidate,
        {a.path: (root / a.path).read_bytes() for a in snapshot.artifacts},
        tmp_path / "zero-gp",
    )
    bundle = project(frozen, model, tmp_path / "output", None)
    inputs, _ = load_calculation_input(bundle / "projection-input.json", PreparedInput)
    assert not next(p for p in inputs.players if p.id == "0").priors
    assert any(
        n.kind == "unavailable" and "nonpositive GP" in n.detail
        for n in inputs.notes
        if n.player_id == "0"
    )
    result = json.loads(next((bundle / "results").glob("*.json")).read_text())
    values = {p["id"]: p["fair"] for p in result["valuation"]["players"]}
    assert values.pop("0") is None
    assert all(value is not None for value in values.values())
