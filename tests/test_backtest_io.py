import json
import re
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from test_backtest import replay_fixture

from fba.adapters.auction import auction_players
from fba.adapters.backtest import backtest_file, validate_observation_sources, validate_timeline
from fba.adapters.config import load_config
from fba.adapters.native import NativeKernel
from fba.adapters.snapshots import artifact
from fba.contracts.backtest import ActualArchive, HealthArchive, ReplayInput, ReplayResult
from fba.contracts.base import DataError
from fba.contracts.data import (
    Calibration,
    Game,
    ManualAdjustments,
    Player,
    Provenance,
    RosterRow,
    Snapshot,
)
from fba.contracts.projection import CalculationResult, PlayerValue, Projected, Valuation
from fba.data.codec import canonical, digest


def historical(value):
    if isinstance(value, str):
        if re.fullmatch(r"202[5-7]-[0-9]{2}", value):
            year = int(value[:4]) - 2
            return f"{year}-{(year + 1) % 100:02}"
        return re.sub(r"202[5-7]", lambda m: str(int(m[0]) - 2), value)
    if isinstance(value, list):
        return [historical(v) for v in value]
    if isinstance(value, dict):
        return {k: historical(v) for k, v in value.items()}
    return value


@pytest.fixture
def frozen_replay(tmp_path):
    native = NativeKernel()
    try:
        source, auction = replay_fixture(native)
    finally:
        native.close()
    source = ReplayInput.model_validate_json(json.dumps(historical(source.model_dump(mode="json"))))
    auction = type(auction).model_validate_json(
        json.dumps(historical(auction.model_dump(mode="json")))
    )
    league, season, model = source.config.league, source.config.season, source.config.model
    season = season.model_copy(
        update={"starts_on": league.matchups[0].start, "ends_on": league.matchups[-1].end}
    )
    league = league.model_copy(
        update={
            "transactions": league.transactions.model_copy(
                update={
                    "add_periods": tuple(
                        p
                        for p in league.transactions.add_periods
                        if season.starts_on <= p.start <= season.ends_on
                    )
                }
            )
        }
    )
    config_root = tmp_path / "config"
    config_root.mkdir()
    for name, value in [("league", league), ("season", season), ("model", model)]:
        (config_root / f"{name}.json").write_bytes(canonical(value))
    config = load_config(*(config_root / f"{n}.json" for n in ("league", "season", "model")))
    files = {
        f"config/{n}.json": (config_root / f"{n}.json").read_bytes()
        for n in ("league", "season", "model")
    }
    zone = ZoneInfo(league.timezone)
    dates = tuple(t.date() for t in source.decision_times)
    times = tuple(datetime.combine(d, time.min, zone) for d in dates)
    source = source.model_copy(update={"config": config, "decision_times": times})
    players = tuple(
        Player(
            roster=RosterRow(
                id=p.id,
                name=p.name,
                positions=p.positions,
                rank=i + 1,
                projected_price=p.projected_price,
                average_price=None,
            ),
            identities=(),
            team_id="nba",
            history_status="available",
        )
        for i, p in enumerate(auction.players)
    )
    snapshot = Snapshot(
        format_version=1,
        season_id=season.season_id,
        version=1,
        as_of=season.snapshot_as_of,
        config=config.refs,
        artifacts=(),
        players=players,
        schedule=tuple(
            Game(
                id=str(i),
                home_team_id="nba",
                away_team_id="other",
                tipoff=datetime.combine(d, time(12), zone),
                local_date=d,
                source_id="schedule",
                status="completed",
            )
            for i, d in enumerate(dates)
        ),
        schedule_counts=(),
        forecasts=(),
        history=(),
        calibration=Calibration(
            training_season_id=season.previous_season_id,
            method="ordinary_least_squares",
            intercept=0.0,
            slope=1.0,
            sample_size=2,
            inputs_sha256=("0" * 64,),
        ),
        adjustments=ManualAdjustments(format_version=1, adjustments=()),
    )
    result = CalculationResult(
        format_version=1,
        algorithm="synthetic-fixture",
        input_sha256="0" * 64,
        config=config.refs,
        projections=tuple(
            Projected(
                id=p.id,
                expected_games=p.expected_games,
                minutes=1.0,
                stats=p.means,
                covariance=p.covariance,
            )
            for p in auction.management.players
        ),
        valuation=Valuation(
            replacement_score=0.0,
            players=tuple(
                PlayerValue(
                    id=p.id, fair=p.fair, utility=p.utility, categories=(), unavailable_reason=None
                )
                for p in auction.players
            ),
        ),
    )
    auction_files = files | {
        "source/snapshot.json": canonical(snapshot),
        "source/calculation.json": canonical(result),
    }
    auction = auction.model_copy(
        update={
            "config": config,
            "players": auction_players(snapshot, result),
            "snapshot_sha256": digest(canonical(snapshot)),
            "calculation_sha256": digest(canonical(result)),
            "artifacts": tuple(artifact(n, b) for n, b in sorted(auction_files.items())),
        }
    )
    files.update({f"auction/{n}": b for n, b in auction_files.items()})
    files["auction/auction-input.json"] = canonical(auction)
    files["health.json"] = canonical(HealthArchive(format_version=1, observations=source.health))
    files["actual.json"] = canonical(ActualArchive(format_version=1, boxes=source.actual))
    entries = []
    for name, data in sorted(files.items()):
        provenance = (
            Provenance(
                source_id=name,
                url="fixture://normalized/" + name,
                raw_sha256=digest(data),
                available_as_of=source.evaluated_at,
                retrieved_at=source.evaluated_at,
                delivery="manual",
            )
            if name in ("health.json", "actual.json")
            else None
        )
        entries.append(artifact(name, data).model_copy(update={"provenance": provenance}))
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    source = source.model_copy(
        update={"artifacts": tuple(entries), "auction_sha256": digest(canonical(auction))}
    )
    path = tmp_path / "replay-input.json"
    path.write_bytes(canonical(source))
    return path, source, auction, snapshot


def test_frozen_historical_replay_and_rebuild(frozen_replay, tmp_path):
    path, source, _, _ = frozen_replay
    first = backtest_file(path, tmp_path / "one")
    second = backtest_file(path, tmp_path / "two")
    assert first.read_bytes() == second.read_bytes()
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "fba.apps.cli",
            "backtest",
            str(path),
            "--output",
            str(tmp_path / "cli"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cli.returncode == 0, cli.stderr
    assert Path(json.loads(cli.stdout)["result"]).read_bytes() == first.read_bytes()
    result = ReplayResult.model_validate_json(first.read_bytes())
    assert result.config == source.config.refs
    assert result.events and result.outcomes and result.champion
    assert list((tmp_path / "one/executions").glob("replay-*.json"))
    altered = source.model_copy(update={"actual": source.actual[:-1]})
    path.write_bytes(canonical(altered))
    with pytest.raises(DataError, match="differs from frozen"):
        backtest_file(path, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_timeline_requires_published_projection_and_timely_decisions(frozen_replay):
    _, source, auction, snapshot = frozen_replay
    late = tuple(t.replace(hour=23) for t in source.decision_times)
    with pytest.raises(DataError, match="cutoff"):
        validate_timeline(source.model_copy(update={"decision_times": late}), auction, snapshot)
    changed = source.model_copy(
        update={
            "config": source.config.model_copy(
                update={
                    "season": source.config.season.model_copy(
                        update={"snapshot_as_of": source.evaluated_at}
                    )
                }
            )
        }
    )
    with pytest.raises(DataError, match="unavailable before first decision"):
        validate_timeline(changed, auction, snapshot)


def test_source_values_are_bound_to_hashed_bytes(frozen_replay):
    path, source, _, _ = frozen_replay
    changed = source.model_copy(
        update={
            "health": tuple(
                e.model_copy(update={"available": not e.available}) for e in source.health
            )
        }
    )
    with pytest.raises(DataError, match="differs from frozen"):
        validate_observation_sources(changed, path.parent)


def test_completed_season_boundary_uses_league_timezone(frozen_replay, tmp_path):
    from datetime import timedelta

    from fba.adapters.calculation import require_completed_season

    path, source, _, _ = frozen_replay
    end = datetime.combine(
        source.config.season.ends_on + timedelta(days=1),
        time.min,
        ZoneInfo(source.config.league.timezone),
    )
    with pytest.raises(DataError, match="has not ended"):
        require_completed_season(source.config, end - timedelta(microseconds=1))
    require_completed_season(source.config, end)
    future = source.model_copy(update={"evaluated_at": end.replace(year=2099)})
    path.write_bytes(canonical(future))
    with pytest.raises(DataError, match="future evaluation"):
        backtest_file(path, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_management_parameters_reject_missing_and_conflicting_values():
    from datetime import timedelta

    from pydantic import ValidationError
    from test_auction import config

    from fba.contracts.base import ConfigError
    from fba.contracts.config import ModelDocument
    from fba.core.config import validate_config

    c = config()
    document = c.model.model_dump(mode="json")
    for field in document["management"]:
        altered = dict(document["management"])
        del altered[field]
        with pytest.raises(ValidationError):
            ModelDocument.model_validate_json(json.dumps(document | {"management": altered}))
    for change in (
        {"reserve_adds": c.league.transactions.adds_per_period + 1},
        {"long_forecast_days": c.model.fit.forecast_days - 1},
        {
            "evidence": c.model.management.evidence.model_copy(
                update={"as_of": c.season.snapshot_as_of + timedelta(seconds=1)}
            )
        },
    ):
        with pytest.raises(ConfigError, match="model.management"):
            validate_config(
                c.league,
                c.season,
                c.model.model_copy(
                    update={"management": c.model.management.model_copy(update=change)}
                ),
                c.refs,
            )
