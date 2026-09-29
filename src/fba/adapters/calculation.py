import os
import tempfile
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError

from fba.adapters.codec import canonical, checked_json, decode, digest, read_bytes
from fba.adapters.config import load_config
from fba.adapters.snapshots import checked_path, load_snapshot
from fba.contracts.base import DataError, Record
from fba.contracts.config import ValidatedConfig
from fba.contracts.projection import (
    BudgetedInput,
    CalibratedData,
    EvaluationInput,
    FrozenCalculationInput,
    OffenseInput,
    ProductionInput,
)
from fba.core.calculation import calculate
from fba.core.evaluation import evaluate


def load_calculation_input[T: FrozenCalculationInput](path: Path, model: type[T]) -> tuple[T, str]:
    data = read_bytes(path)
    inputs = decode(model, data, str(path))
    verify_calculation_input(inputs, path)
    return inputs, digest(data)


def load_projection(path: Path) -> tuple[ProductionInput, str]:
    data = read_bytes(path)
    inputs = decode_projection(data, str(path))
    verify_calculation_input(inputs, path)
    return inputs, digest(data)


def decode_projection(data: bytes, source: str) -> ProductionInput:
    try:
        return TypeAdapter[ProductionInput](ProductionInput).validate_json(
            checked_json(data, source)
        )
    except ValidationError as exc:
        raise DataError(f"{source}: {exc}") from exc


def verify_calculation_input(
    inputs: FrozenCalculationInput, path: Path, *, source_cutoff: datetime | None = None
) -> None:
    cutoff = inputs.config.season.snapshot_as_of if source_cutoff is None else source_cutoff
    paths = tuple(a.path for a in inputs.artifacts)
    if len(set(paths)) != len(paths):
        raise DataError("projection.artifacts: duplicate paths")
    required = {f"config/{name}.json" for name in ("league", "season", "model")}
    if not required <= set(paths):
        raise DataError("projection.artifacts: frozen configuration files are required")
    for artifact in inputs.artifacts:
        payload = read_bytes(checked_path(path.parent, artifact.path))
        if digest(payload) != artifact.sha256 or len(payload) != artifact.size:
            raise DataError(f"projection.artifacts.{artifact.path}: SHA-256 or size mismatch")
        p = artifact.provenance
        if p is not None and (
            p.raw_sha256 != artifact.sha256 or p.retrieved_at > cutoff or p.available_as_of > cutoff
        ):
            raise DataError(f"projection.artifacts.{artifact.path}: invalid provenance")
    effective = load_config(
        *(path.parent / f"config/{name}.json" for name in ("league", "season", "model"))
    )
    if effective != inputs.config:
        raise DataError("projection.config: frozen hashes or values disagree")
    if isinstance(inputs, CalibratedData):
        validate_calibration_snapshot(inputs, path.parent)
    if isinstance(inputs, BudgetedInput):
        # Import here because source parsing also validates archived forecasts.
        from fba.adapters.team_minutes import team_members

        snapshot_root = checked_path(path.parent, inputs.calibration_snapshot)
        snapshot = load_snapshot(snapshot_root)
        if team_members(snapshot_root, snapshot, inputs.config) != inputs.team_members:
            raise DataError("team_minutes: population differs from frozen sources")
        if isinstance(inputs, OffenseInput):
            from fba.adapters.team_offense import offense_sources

            outside, baselines = offense_sources(
                snapshot_root, snapshot, inputs.config, inputs.team_members
            )
            if outside != inputs.outside_priors or baselines != inputs.offense_baselines:
                raise DataError("team_offense: priors or baselines differ from frozen sources")


def validate_calibration_snapshot(inputs: CalibratedData, root: Path) -> None:
    snapshot = load_snapshot(checked_path(root, inputs.calibration_snapshot))
    required = {
        f"{inputs.calibration_snapshot}/{p}"
        for p in ("snapshot.json", *(a.path for a in snapshot.artifacts))
    }
    if not required <= {a.path for a in inputs.artifacts}:
        raise DataError("calibration_snapshot: incomplete frozen artifact inventory")
    if (
        snapshot.calibration != inputs.calibration
        or snapshot.season_id != inputs.config.season.season_id
        or snapshot.as_of > inputs.config.season.snapshot_as_of
        or not set(inputs.calibration.inputs_sha256) <= {a.sha256 for a in snapshot.artifacts}
    ):
        raise DataError("calibration_snapshot: mismatched fit, season, or future information")


def calculate_file(path: Path, output: Path) -> Path:
    inputs, input_hash = load_projection(path)
    return publish_result(calculate(inputs, input_hash), output, "calculation")


def evaluate_file(path: Path, output: Path) -> Path:
    inputs, input_hash = load_calculation_input(path, EvaluationInput)
    require_completed_season(inputs.config, datetime.now(UTC))
    return publish_result(evaluate(inputs, input_hash), output, "evaluation")


def require_completed_season(config: ValidatedConfig, evaluated_at: datetime) -> None:
    end = datetime.combine(
        config.season.ends_on + timedelta(days=1), time.min, ZoneInfo(config.league.timezone)
    )
    if evaluated_at < end:
        raise DataError("evaluation: season has not ended in league timezone")


def publish_result(record: Record, output: Path, prefix: str) -> Path:
    result = canonical(record)
    destination = output / f"{prefix}-{digest(result)}.json"
    temporary: str | None = None
    try:
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=output, prefix=".calculating-", delete=False
        ) as stream:
            temporary = stream.name
            stream.write(result)
        # A hard link publishes atomically and cannot overwrite an existing result.
        os.link(temporary, destination)
    except OSError as exc:
        raise DataError(f"{destination}: result publication failed: {exc}") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return destination
