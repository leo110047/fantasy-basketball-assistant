import json
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from fba.adapters.acquisition import Acquired
from fba.adapters.codec import canonical, decode, digest, read_bytes
from fba.contracts.base import DataError, VersionError
from fba.contracts.config import ValidatedConfig
from fba.contracts.data import Artifact, Snapshot


def artifact(path: str, data: bytes, source: Acquired | None = None) -> Artifact:
    return Artifact(
        path=path,
        sha256=digest(data),
        size=len(data),
        provenance=source.provenance if source is not None else None,
    )


def checked_path(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts or not parts.parts or "\\" in relative:
        raise DataError(f"snapshot.artifacts: unsafe path {relative}")
    result = root / relative
    if not result.resolve().is_relative_to(root.resolve()) or result.is_symlink():
        raise DataError(f"snapshot.artifacts: path escapes snapshot {relative}")
    return result


def publish(snapshot: Snapshot, files: dict[str, bytes], output: Path) -> Path:
    """Publish only a fully verified tree; existing versions are never overwritten."""
    payload = canonical(snapshot)
    name = f"snapshot-{digest(payload)}"
    output.mkdir(parents=True, exist_ok=True)
    destination = output / name
    if destination.exists():
        raise DataError(f"{destination}: snapshot already exists")
    if set(files) != {a.path for a in snapshot.artifacts}:
        raise DataError("snapshot.artifacts: file inventory mismatch")
    temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=output))
    try:
        for entry in snapshot.artifacts:
            data = files[entry.path]
            if len(data) != entry.size or digest(data) != entry.sha256:
                raise DataError(f"snapshot.artifacts.{entry.path}: bytes do not match manifest")
            path = checked_path(temporary, entry.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (temporary / "snapshot.json").write_bytes(payload)
        verify_files(temporary, snapshot)
        # macOS and Linux reject replacing a nonempty snapshot directory.
        temporary.rename(destination)
    except (OSError, DataError) as exc:
        shutil.rmtree(temporary)
        if isinstance(exc, DataError):
            raise
        raise DataError(f"{destination}: snapshot publication failed: {exc}") from exc
    return destination


def verify_files(root: Path, snapshot: Snapshot) -> None:
    paths = tuple(a.path for a in snapshot.artifacts)
    if len(paths) != len(set(paths)) or "snapshot.json" in paths:
        raise DataError("snapshot.artifacts: duplicate or reserved path")
    for entry in snapshot.artifacts:
        data = read_bytes(checked_path(root, entry.path))
        if digest(data) != entry.sha256 or len(data) != entry.size:
            raise DataError(f"snapshot.artifacts.{entry.path}: SHA-256 or size mismatch")
        provenance = entry.provenance
        if provenance is not None and (
            provenance.raw_sha256 != entry.sha256
            or provenance.available_as_of > snapshot.as_of
            or provenance.retrieved_at > snapshot.as_of
        ):
            raise DataError(f"snapshot.artifacts.{entry.path}: invalid provenance")


def load_snapshot(root: Path) -> Snapshot:
    data = read_bytes(root / "snapshot.json")
    if root.name != f"snapshot-{digest(data)}":
        raise DataError(f"{root}: snapshot manifest SHA-256 mismatch")
    # Report unsupported versions separately from malformed supported records.
    from pydantic import JsonValue, TypeAdapter

    parsed = TypeAdapter(dict[str, JsonValue]).validate_json(data)
    if parsed.get("format_version") != 1:
        raise VersionError(f"{root}: unsupported snapshot format_version")
    snapshot = decode(Snapshot, data, str(root / "snapshot.json"))
    verify_files(root, snapshot)
    return snapshot


def frozen_inputs(
    config: ValidatedConfig,
    sources: tuple[Acquired, ...],
    inputs: dict[str, bytes],
) -> tuple[tuple[Artifact, ...], dict[str, bytes]]:
    files = dict(inputs)
    files["config/effective.json"] = canonical(config)
    artifacts = [artifact(path, data) for path, data in files.items()]
    for acquired in sources:
        path = f"raw/{acquired.source.id}.json"
        if path in files:
            raise DataError(f"{path}: duplicate source")
        files[path] = acquired.data
        artifacts.append(artifact(path, acquired.data, acquired))
    return tuple(sorted(artifacts, key=lambda a: a.path)), files


def inventory_json(snapshot: Snapshot) -> str:
    missing = [
        {"player_id": f.player_id, "stat": s.id, "reason": s.missing_reason}
        for f in snapshot.forecasts
        for s in f.totals
        if s.value is None
    ]
    return json.dumps(
        {
            "season_id": snapshot.season_id,
            "players": len(snapshot.players),
            "forecasts": len(snapshot.forecasts),
            "games": len(snapshot.schedule),
            "player_games": len(snapshot.history),
            "missing_projection_stats": missing,
            "missing_forecast_ids": [
                p.roster.id
                for p in snapshot.players
                if p.roster.id not in {f.player_id for f in snapshot.forecasts}
            ],
            "calibration": snapshot.calibration.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
