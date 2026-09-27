import os
import tempfile
from pathlib import Path

from fba.adapters.codec import canonical, decode, digest, read_bytes
from fba.adapters.config import load_config
from fba.adapters.snapshots import checked_path
from fba.contracts.base import DataError, Record
from fba.contracts.projection import EvaluationInput, FrozenCalculationInput, ProjectionInput
from fba.core.calculation import calculate
from fba.core.evaluation import evaluate


def load_calculation_input[T: FrozenCalculationInput](path: Path, model: type[T]) -> tuple[T, str]:
    data = read_bytes(path)
    inputs = decode(model, data, str(path))
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
            p.raw_sha256 != artifact.sha256
            or p.retrieved_at > inputs.config.season.snapshot_as_of
            or p.available_as_of > inputs.config.season.snapshot_as_of
        ):
            raise DataError(f"projection.artifacts.{artifact.path}: invalid provenance")
    effective = load_config(
        *(path.parent / f"config/{name}.json" for name in ("league", "season", "model"))
    )
    if effective != inputs.config:
        raise DataError("projection.config: frozen hashes or values disagree")
    return inputs, digest(data)


def calculate_file(path: Path, output: Path) -> Path:
    inputs, input_hash = load_calculation_input(path, ProjectionInput)
    return publish_result(calculate(inputs, input_hash), output, "calculation")


def evaluate_file(path: Path, output: Path) -> Path:
    inputs, input_hash = load_calculation_input(path, EvaluationInput)
    return publish_result(evaluate(inputs, input_hash), output, "evaluation")


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
