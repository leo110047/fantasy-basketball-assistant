from pathlib import Path

from fba.adapters.calculation import load_calculation_input
from fba.adapters.codec import canonical, digest, read_bytes
from fba.adapters.config import load_parameters
from fba.adapters.snapshots import artifact, publish_bundle
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import CalculationModel, ResourceModel
from fba.contracts.projection import (
    ProjectionInput,
    ProjectionPlayer,
    ProjectionTeam,
    ResourceInput,
)
from fba.core.config import validate_config


def migrate_projection(path: Path, model_path: Path, output: Path) -> Path:
    """Explicitly replace the resource model; preserve its original input for audit."""
    old, input_hash = load_calculation_input(path, ResourceInput)
    model, ref = load_parameters(model_path)
    config = validate_config(
        old.config.league,
        old.config.season,
        model,
        old.config.refs.model_copy(update={"model": ref}),
    )
    if not isinstance(config.model, CalculationModel):
        raise ConfigError("migration.model: requires a current calculation model")
    if not isinstance(old.config.model, ResourceModel) or (
        old.config.model.projection.stat_ids != config.model.projection.stat_ids
    ):
        raise ConfigError("migration.model.projection.stat_ids: must preserve the original axes")
    files = {a.path: read_bytes(path.parent / a.path) for a in old.artifacts}
    for entry in old.artifacts:
        if digest(files[entry.path]) != entry.sha256 or len(files[entry.path]) != entry.size:
            raise DataError(f"migration.{entry.path}: source changed during conversion")
    if {"migration/input-v1.json", "migration/model-v2.json"} & set(files):
        raise DataError("migration.artifacts: reserved destination paths already exist")
    files["migration/input-v1.json"] = read_bytes(path)
    files["migration/model-v2.json"] = files["config/model.json"]
    files["config/model.json"] = read_bytes(model_path)
    if (
        digest(files["migration/input-v1.json"]) != input_hash
        or digest(files["config/model.json"]) != config.refs.model.input_sha256
    ):
        raise DataError("migration: input changed during conversion")
    if "config/effective.json" in files:
        files["config/effective.json"] = canonical(config)
    source = {a.path: a for a in old.artifacts if a.provenance is not None}
    artifacts = tuple(
        source[name] if name in source else artifact(name, data)
        for name, data in sorted(files.items())
    )
    candidate = ProjectionInput(
        format_version=2,
        config=config,
        artifacts=artifacts,
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
            for p in old.players
            if p.catalog
        ),
        teams=tuple(
            ProjectionTeam(id=t.id, dates=t.dates, full_season_games=t.full_season_games)
            for t in old.teams
        ),
    )
    return publish_bundle(
        candidate, artifacts, config.season.snapshot_as_of, files, output, "projection-input"
    )
