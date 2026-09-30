from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter_ns
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from fba.adapters.auction import load_auction
from fba.adapters.calculation import (
    publish_result,
    require_completed_season,
    verify_calculation_input,
)
from fba.adapters.native import NativeKernel
from fba.adapters.snapshots import checked_path
from fba.auction.managed import management_calendar
from fba.auction.season import replay
from fba.contracts.auction import AuctionInput
from fba.contracts.backtest import (
    ActualArchive,
    HealthArchive,
    ReplayDocument,
    ReplayInput,
    ScheduleArchive,
    ScheduledGame,
    ScheduledReplayInput,
)
from fba.contracts.base import DataError, FormatVersion, Natural, Record
from fba.contracts.data import Digest, Snapshot
from fba.core.replay_schedule import season_days
from fba.data.codec import canonical, checked_json, decode, digest, read_bytes


class ReplayExecution(Record):
    format_version: FormatVersion
    input_sha256: Digest
    result_sha256: Digest
    native_source_sha256: Digest
    native_binary_sha256: Digest
    elapsed_ns: Natural


def validate_timeline(inputs: ReplayInput, auction: AuctionInput, snapshot: Snapshot) -> None:
    if auction.management is None:
        raise DataError("replay.auction: missing management inputs")
    calendar = sorted({d for p in auction.management.players for d in p.game_days})
    if not calendar:
        raise DataError("replay.schedule: empty calendar")
    days = (
        season_days(inputs.config.season)
        if isinstance(inputs, ScheduledReplayInput)
        else management_calendar(inputs.config.league, tuple(calendar))
    )
    zone = ZoneInfo(inputs.config.league.timezone)
    if tuple(t.astimezone(zone).date() for t in inputs.decision_times) != days:
        raise DataError("replay.decision_times: must be chronological and cover each local date")
    first_game: dict[date, datetime] = {}
    for game in snapshot.schedule:
        old = first_game.get(game.local_date)
        first_game[game.local_date] = game.tipoff if old is None else min(old, game.tipoff)
    if isinstance(inputs, ScheduledReplayInput):
        first_game = known_tipoffs(inputs, auction, snapshot)
    cutoff = min(
        inputs.config.league.lineup.lock_local_time,
        inputs.config.league.transactions.cutoff_local_time,
    )
    for day, when in zip(days, inputs.decision_times, strict=True):
        deadline = datetime.combine(day, cutoff, zone)
        if when > deadline or (day in first_game and when >= first_game[day]):
            raise DataError(f"replay.decision_times.{day}: after lineup/add cutoff or first game")
    require_completed_season(inputs.config, inputs.evaluated_at)
    if (
        inputs.information_mode == "published"
        and inputs.config.season.snapshot_as_of >= inputs.decision_times[0]
    ):
        raise DataError(
            "replay.published: projection snapshot was unavailable before first decision"
        )
    artifacts = {a.path: a for a in inputs.artifacts}
    schedules = inputs.schedules if isinstance(inputs, ScheduledReplayInput) else ()
    for row in (*inputs.health, *inputs.actual, *schedules):
        artifact = artifacts.get(row.source_artifact)
        if artifact is None or artifact.provenance is None:
            raise DataError(
                "replay.sources: each observation requires a frozen source with provenance"
            )
    if any(e.published_at > inputs.evaluated_at for e in inputs.health):
        raise DataError("replay.health.published_at: after evaluation date")


def known_tipoffs(
    inputs: ScheduledReplayInput, auction: AuctionInput, snapshot: Snapshot
) -> dict[date, datetime]:
    assert auction.management is not None
    zone = ZoneInfo(inputs.config.league.timezone)
    teams = {p.roster.id: p.team_id for p in snapshot.players}
    current: dict[str, tuple[ScheduledGame, ...]] = {}
    for player in auction.management.players:
        games = tuple(
            ScheduledGame(day=g.local_date, tipoff=g.tipoff)
            for g in snapshot.schedule
            if teams.get(player.id) in (g.home_team_id, g.away_team_id)
            and g.local_date in player.game_days
            and g.status in ("scheduled", "completed")
        )
        if len(games) != len(player.game_days) or {g.day for g in games} != set(player.game_days):
            raise DataError("replay.schedules: initial player dates require exact sourced tipoffs")
        current[player.id] = games
    events = sorted(inputs.schedules, key=lambda e: (e.published_at, e.player_id))
    for event in events:
        if event.player_id not in current or any(
            g.tipoff.astimezone(zone).date() != g.day for g in event.games
        ):
            raise DataError("replay.schedules: unknown player or inconsistent local tipoff date")
    first: dict[date, datetime] = {}
    cursor = 0
    for when in inputs.decision_times:
        while cursor < len(events) and events[cursor].published_at <= when:
            event = events[cursor]
            current[event.player_id] = event.games
            cursor += 1
        day = when.astimezone(zone).date()
        starts = tuple(g.tipoff for games in current.values() for g in games if g.day == day)
        if starts:
            first[day] = min(starts)
    return first


def localize_replay(inputs: ReplayInput) -> ReplayInput:
    if not isinstance(inputs, ScheduledReplayInput):
        return inputs
    zone = ZoneInfo(inputs.config.league.timezone)
    return inputs.model_copy(
        update={
            "decision_times": tuple(t.astimezone(zone) for t in inputs.decision_times),
            "schedules": tuple(
                e.model_copy(update={"published_at": e.published_at.astimezone(zone)})
                for e in inputs.schedules
            ),
        }
    )


def validate_observation_sources(inputs: ReplayInput, root: Path) -> None:
    for name in sorted({e.source_artifact for e in inputs.health}):
        archive = decode(HealthArchive, read_bytes(checked_path(root, name)), name)
        expected = tuple(e for e in inputs.health if e.source_artifact == name)
        if set(archive.observations) != set(expected) or len(archive.observations) != len(expected):
            raise DataError(f"replay.health.{name}: differs from frozen observations")
    for name in sorted({e.source_artifact for e in inputs.actual}):
        archive = decode(ActualArchive, read_bytes(checked_path(root, name)), name)
        expected = tuple(e for e in inputs.actual if e.source_artifact == name)
        if set(archive.boxes) != set(expected) or len(archive.boxes) != len(expected):
            raise DataError(f"replay.actual.{name}: differs from frozen box scores")
    if isinstance(inputs, ScheduledReplayInput):
        for name in sorted({e.source_artifact for e in inputs.schedules}):
            archive = decode(ScheduleArchive, read_bytes(checked_path(root, name)), name)
            expected = tuple(e for e in inputs.schedules if e.source_artifact == name)
            if set(archive.observations) != set(expected) or len(archive.observations) != len(
                expected
            ):
                raise DataError(f"replay.schedules.{name}: differs from frozen observations")


def backtest_file(path: Path, output: Path) -> Path:
    payload = read_bytes(path)
    try:
        inputs = ReplayDocument.model_validate_json(checked_json(payload, str(path))).root
    except ValidationError as exc:
        raise DataError(f"{path}: {exc}") from exc
    input_hash = digest(payload)
    verify_calculation_input(inputs, path, source_cutoff=inputs.evaluated_at)
    auction_path = checked_path(path.parent, inputs.auction_path)
    auction, auction_hash = load_auction(auction_path)
    if auction_hash != inputs.auction_sha256:
        raise DataError("replay.auction_sha256: source hash mismatch")
    if inputs.evaluated_at > datetime.now(UTC):
        raise DataError("replay.evaluated_at: future evaluation timestamp")
    snapshot = decode(
        Snapshot, read_bytes(auction_path.parent / "source/snapshot.json"), "replay.snapshot"
    )
    validate_timeline(inputs, auction, snapshot)
    validate_observation_sources(inputs, path.parent)
    inputs = localize_replay(inputs)
    started = perf_counter_ns()
    native = NativeKernel()
    try:
        result = replay(inputs, auction, input_hash, native)
        destination = publish_result(result, output, "season")
        publish_result(
            ReplayExecution(
                format_version=1,
                input_sha256=input_hash,
                result_sha256=digest(canonical(result)),
                native_source_sha256=native.artifact.source_sha256,
                native_binary_sha256=native.artifact.binary_sha256,
                elapsed_ns=perf_counter_ns() - started,
            ),
            output / "executions",
            "replay",
        )
        return destination
    finally:
        native.close()
