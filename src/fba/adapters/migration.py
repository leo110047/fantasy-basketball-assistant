from pathlib import Path

from pydantic import JsonValue, TypeAdapter, ValidationError

from fba.adapters.calculation import load_calculation_input
from fba.adapters.codec import canonical, checked_json, digest, read_bytes
from fba.adapters.config import load_parameters
from fba.adapters.snapshots import artifact, checked_path, load_snapshot, publish_bundle
from fba.contracts.base import ConfigError, DataError, VersionError
from fba.contracts.config import CalculationModel, ResourceModel, TeamBudgetModel
from fba.contracts.data import Artifact, Snapshot
from fba.contracts.projection import (
    CalibratedInput,
    ProjectionInput,
    ProjectionPlayer,
    ProjectionTeam,
    ResourceInput,
)
from fba.core.config import validate_config


def legacy_input(path: Path) -> tuple[ResourceInput | ProjectionInput, str]:
    try:
        header = TypeAdapter(dict[str, JsonValue]).validate_json(
            checked_json(read_bytes(path), str(path))
        )
    except ValidationError as exc:
        raise DataError(f"{path}: {exc}") from exc
    version = header.get("format_version")
    if version == 1:
        return load_calculation_input(path, ResourceInput)
    if version == 2:
        return load_calculation_input(path, ProjectionInput)
    raise VersionError(f"{path}: migration requires input format_version 1 or 2")


def frozen_calibration(root: Path) -> tuple[Snapshot, str, dict[str, bytes], tuple[Artifact, ...]]:
    snapshot = load_snapshot(root)
    prefix = f"calibration/{root.name}"
    files = {
        f"{prefix}/{a.path}": read_bytes(checked_path(root, a.path)) for a in snapshot.artifacts
    }
    files[f"{prefix}/snapshot.json"] = read_bytes(root / "snapshot.json")
    if digest(files[f"{prefix}/snapshot.json"]) != root.name.removeprefix("snapshot-"):
        raise DataError("calibration_snapshot: manifest changed during conversion")
    artifacts = tuple(
        a.model_copy(update={"path": f"{prefix}/{a.path}"}) for a in snapshot.artifacts
    ) + (artifact(f"{prefix}/snapshot.json", files[f"{prefix}/snapshot.json"]),)
    return snapshot, prefix, files, artifacts


def migrate_projection(path: Path, model_path: Path, calibration_root: Path, output: Path) -> Path:
    """Freeze the explicit new model and calibration with the original input for audit."""
    old, input_hash = legacy_input(path)
    model, ref = load_parameters(model_path)
    if isinstance(model, TeamBudgetModel):
        raise ConfigError(
            "migration.model: complete-population models require project from the source snapshot"
        )
    config = validate_config(
        old.config.league,
        old.config.season,
        model,
        old.config.refs.model_copy(update={"model": ref}),
    )
    if not isinstance(config.model, CalculationModel):
        raise ConfigError("migration.model: requires a current calculation model")
    if not isinstance(old.config.model, (ResourceModel, CalculationModel)) or (
        old.config.model.projection.stat_ids != config.model.projection.stat_ids
    ):
        raise ConfigError("migration.model.projection.stat_ids: must preserve the original axes")
    files = {a.path: read_bytes(path.parent / a.path) for a in old.artifacts}
    for entry in old.artifacts:
        if digest(files[entry.path]) != entry.sha256 or len(files[entry.path]) != entry.size:
            raise DataError(f"migration.{entry.path}: source changed during conversion")
    input_archive = f"migration/input-v{old.format_version}.json"
    model_archive = f"migration/model-v{old.config.model.format_version}.json"
    if {input_archive, model_archive} & set(files):
        raise DataError("migration.artifacts: reserved destination paths already exist")
    files[input_archive] = read_bytes(path)
    files[model_archive] = files["config/model.json"]
    files["config/model.json"] = read_bytes(model_path)
    if (
        digest(files[input_archive]) != input_hash
        or digest(files["config/model.json"]) != config.refs.model.input_sha256
    ):
        raise DataError("migration: input changed during conversion")
    if "config/effective.json" in files:
        files["config/effective.json"] = canonical(config)
    source = {a.path: a for a in old.artifacts if a.provenance is not None}
    snapshot, prefix, calibration_files, calibration_artifacts = frozen_calibration(
        calibration_root
    )
    if (
        snapshot.season_id != config.season.season_id
        or snapshot.as_of > config.season.snapshot_as_of
        or snapshot.calibration.training_season_id != config.season.previous_season_id
    ):
        raise DataError("calibration_snapshot: wrong season or after snapshot cutoff")
    if set(files) & set(calibration_files):
        raise DataError("calibration_snapshot: reserved destination paths already exist")
    files.update(calibration_files)
    source.update((a.path, a) for a in calibration_artifacts)
    artifacts = tuple(
        source[name] if name in source else artifact(name, data)
        for name, data in sorted(files.items())
    )
    players = (
        tuple(p for p in old.players if p.catalog)
        if isinstance(old, ResourceInput)
        else old.players
    )
    candidate = CalibratedInput(
        format_version=3,
        config=config,
        artifacts=artifacts,
        calibration=snapshot.calibration,
        calibration_snapshot=prefix,
        players=tuple(
            ProjectionPlayer(
                id=p.id,
                name=p.name,
                team_id=p.team_id,
                priors=p.priors,
                games_cap=p.games_cap,
                return_on=p.return_on,
                history=p.history,
            )
            for p in players
        ),
        teams=tuple(
            ProjectionTeam(id=t.id, dates=t.dates, full_season_games=t.full_season_games)
            for t in old.teams
        ),
    )
    return publish_bundle(
        candidate, artifacts, config.season.snapshot_as_of, files, output, "projection-input"
    )
