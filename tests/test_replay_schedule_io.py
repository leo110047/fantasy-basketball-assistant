import json
import subprocess
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from test_backtest_io import frozen_replay as frozen_replay

from fba.adapters.backtest import backtest_file, localize_replay, validate_timeline
from fba.adapters.snapshots import artifact
from fba.contracts.backtest import (
    ActualArchive,
    ReplayDocument,
    ReplayResult,
    ScheduleArchive,
    ScheduledGame,
    ScheduledReplayInput,
    ScheduleObservation,
)
from fba.contracts.base import DataError
from fba.contracts.data import Provenance
from fba.core.replay_schedule import season_days
from fba.data.codec import canonical, digest


@pytest.fixture
def frozen_schedule(frozen_replay):
    path, source, auction, snapshot = frozen_replay
    zone = ZoneInfo(source.config.league.timezone)
    player = auction.management.players[0]
    old, new = player.game_days[0], source.config.season.ends_on
    assert new not in player.game_days
    publication = datetime.combine(old, time(23), zone).astimezone(UTC)
    assert publication.date() != old  # UTC has rolled over; league local time has not.
    event = ScheduleObservation(
        player_id=player.id,
        published_at=publication,
        games=tuple(
            ScheduledGame(day=d, tipoff=datetime.combine(d, time(12), zone))
            for d in sorted((set(player.game_days) - {old}) | {new})
        ),
        source_artifact="schedule.json",
    )
    current = ScheduledReplayInput(
        **{
            **source.model_dump(),
            "format_version": 2,
            "schedules": (event,),
            "decision_times": tuple(
                datetime.combine(d, time.min, zone).astimezone(UTC)
                for d in season_days(source.config.season)
            ),
            "actual": tuple(
                b.model_copy(update={"day": new}) if (b.player_id, b.day) == (player.id, old) else b
                for b in source.actual
            ),
        }
    )
    files = {
        "schedule.json": canonical(
            ScheduleArchive(format_version=1, observations=current.schedules)
        ),
        "actual.json": canonical(ActualArchive(format_version=1, boxes=current.actual)),
    }
    entries = [a for a in current.artifacts if a.path not in files]
    for name, data in files.items():
        (path.parent / name).write_bytes(data)
        provenance = Provenance(
            source_id=name,
            url="fixture://normalized/" + name,
            raw_sha256=digest(data),
            available_as_of=current.evaluated_at,
            retrieved_at=current.evaluated_at,
            delivery="manual",
        )
        entries.append(artifact(name, data).model_copy(update={"provenance": provenance}))
    current = current.model_copy(update={"artifacts": tuple(sorted(entries, key=lambda a: a.path))})
    path.write_bytes(canonical(current))
    return path, current, auction, snapshot


def test_frozen_v2_cli_rebuild_and_local_publication_date(frozen_schedule, tmp_path):
    path, source, _, _ = frozen_schedule
    assert isinstance(
        ReplayDocument.model_validate_json(path.read_bytes()).root, ScheduledReplayInput
    )
    localized = localize_replay(source)
    assert (
        localized.schedules[0].published_at.date()
        == source.decision_times[0].astimezone(ZoneInfo(source.config.league.timezone)).date()
    )
    archived = {a.path: (path.parent / a.path).read_bytes() for a in source.artifacts}
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
    assert result.days == season_days(source.config.season)
    assert result.algorithm == "published-schedule-management-v1"
    assert result.input_sha256 == digest(path.read_bytes())
    assert result.events and result.outcomes and result.champion
    assert all((path.parent / name).read_bytes() == data for name, data in archived.items())


@pytest.mark.parametrize("failure", ["unbound", "tampered", "missing_provenance", "bad_tipoff"])
def test_v2_rejects_invalid_frozen_schedules(frozen_schedule, tmp_path, failure):
    path, source, _, _ = frozen_schedule
    event = source.schedules[0]
    if failure == "unbound":
        source = source.model_copy(
            update={"schedules": (event.model_copy(update={"games": event.games[:-1]}),)}
        )
        message = "differs from frozen"
    elif failure == "tampered":
        (path.parent / "schedule.json").write_bytes(b"{}")
        message = "SHA-256 or size mismatch"
    elif failure == "missing_provenance":
        source = source.model_copy(
            update={
                "artifacts": tuple(
                    a.model_copy(update={"provenance": None}) if a.path == "schedule.json" else a
                    for a in source.artifacts
                )
            }
        )
        message = "requires a frozen source"
    else:
        game = event.games[0].model_copy(
            update={"tipoff": event.games[0].tipoff + timedelta(days=1)}
        )
        event = event.model_copy(update={"games": (game, *event.games[1:])})
        source = source.model_copy(update={"schedules": (event,)})
        message = "local tipoff date"
    path.write_bytes(canonical(source))
    with pytest.raises(DataError, match=message):
        backtest_file(path, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_decision_cutoff_uses_only_tipoffs_public_at_that_time(frozen_schedule):
    _, source, auction, snapshot = frozen_schedule
    player = auction.management.players[0]
    first = source.decision_times[0]
    local_day = first.astimezone(ZoneInfo(source.config.league.timezone)).date()
    assert local_day in player.game_days
    event = source.schedules[0].model_copy(
        update={
            "published_at": first - timedelta(hours=1),
            "games": tuple(
                ScheduledGame(
                    day=d,
                    tipoff=first
                    if d == local_day
                    else datetime.combine(d, time(12), ZoneInfo(source.config.league.timezone)),
                )
                for d in player.game_days
            ),
        }
    )
    # A published earlier tipoff makes the existing decision too late.
    with pytest.raises(DataError, match="first game"):
        validate_timeline(source.model_copy(update={"schedules": (event,)}), auction, snapshot)
    # Its future announcement must not retroactively invalidate an earlier decision.
    later = event.model_copy(update={"published_at": first + timedelta(hours=1)})
    validate_timeline(source.model_copy(update={"schedules": (later,)}), auction, snapshot)
