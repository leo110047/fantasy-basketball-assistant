from pathlib import Path

from fba.adapters.codec import digest, read_bytes
from fba.adapters.espn import team_labels
from fba.adapters.snapshots import checked_path
from fba.contracts.base import DataError
from fba.contracts.config import ValidatedConfig
from fba.contracts.data import Artifact, Snapshot, TeamLabel


def label_sources(snapshot: Snapshot, config: ValidatedConfig) -> tuple[Artifact, ...]:
    sources = {s.id: s for s in config.season.sources if s.role == "schedule"}
    artifacts = tuple(
        a for a in snapshot.artifacts if a.provenance and a.provenance.source_id in sources
    )
    if not artifacts or {a.provenance.source_id for a in artifacts if a.provenance} != set(sources):
        raise DataError("auction.teams: missing frozen schedule sources")
    if any(s.adapter != "espn_schedule" for s in sources.values()):
        raise DataError("auction.teams: schedule adapter does not provide team labels")
    return tuple(sorted(artifacts, key=lambda a: a.path))


def freeze_team_sources(
    snapshot: Snapshot, config: ValidatedConfig, root: Path
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for entry in label_sources(snapshot, config):
        data = read_bytes(checked_path(root, entry.path))
        if digest(data) != entry.sha256:
            raise DataError(f"auction.teams: source changed: {entry.path}")
        files[f"source/teams/{entry.path}"] = data
    return files


def auction_teams(
    snapshot: Snapshot, config: ValidatedConfig, files: dict[str, bytes]
) -> tuple[TeamLabel, ...]:
    teams: dict[str, TeamLabel] = {}
    for entry in label_sources(snapshot, config):
        data = files.get(f"source/teams/{entry.path}")
        if data is None or digest(data) != entry.sha256:
            raise DataError(f"auction.teams: missing or changed source: {entry.path}")
        for team in team_labels(data, entry.path):
            if team.id in teams and teams[team.id] != team:
                raise DataError(f"auction.teams: conflicting labels for {team.id}")
            teams[team.id] = team
    missing = {p.team_id for p in snapshot.players if p.team_id is not None} - teams.keys()
    if missing:
        raise DataError(f"auction.teams: missing labels for {sorted(missing)}")
    return tuple(teams[k] for k in sorted(teams))
