from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from zoneinfo import ZoneInfo

from fba.adapters.auction import load_auction
from fba.adapters.calculation import (
    publish_result,
    require_completed_season,
    verify_calculation_input,
)
from fba.adapters.codec import canonical, decode, digest, read_bytes
from fba.adapters.native import NativeKernel
from fba.adapters.snapshots import checked_path
from fba.contracts.auction import AuctionInput
from fba.contracts.backtest import ActualArchive, HealthArchive, ReplayInput
from fba.contracts.base import DataError, FormatVersion, Natural, Record
from fba.contracts.data import Digest, Snapshot
from fba.core.managed import management_calendar
from fba.core.season import replay


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
    days = management_calendar(inputs.config.league, tuple(calendar))
    zone = ZoneInfo(inputs.config.league.timezone)
    if tuple(t.astimezone(zone).date() for t in inputs.decision_times) != days:
        raise DataError("replay.decision_times: must be chronological and cover each local date")
    first_game: dict[object, datetime] = {}
    for game in snapshot.schedule:
        old = first_game.get(game.local_date)
        first_game[game.local_date] = game.tipoff if old is None else min(old, game.tipoff)
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
    for row in (*inputs.health, *inputs.actual):
        artifact = artifacts.get(row.source_artifact)
        if artifact is None or artifact.provenance is None:
            raise DataError(
                "replay.sources: each observation requires a frozen source with provenance"
            )
    if any(e.published_at > inputs.evaluated_at for e in inputs.health):
        raise DataError("replay.health.published_at: after evaluation date")


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


def backtest_file(path: Path, output: Path) -> Path:
    payload = read_bytes(path)
    inputs, input_hash = decode(ReplayInput, payload, str(path)), digest(payload)
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
