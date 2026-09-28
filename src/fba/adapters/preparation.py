import json
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from fba.adapters.annual import forecast_archive
from fba.adapters.calculation import load_projection, publish_result, verify_calculation_input
from fba.adapters.codec import canonical, decode, digest, read_bytes
from fba.adapters.config import load_config, load_parameters
from fba.adapters.migration import frozen_calibration
from fba.adapters.snapshots import artifact, publish_bundle
from fba.contracts.base import ConfigError, DataError
from fba.contracts.config import PreparationModel
from fba.contracts.data import ManualAdjustments, ReturnAt
from fba.contracts.projection import CalculationResult, PreparedInput
from fba.core.calculation import calculate
from fba.core.config import validate_config
from fba.core.preparation import prepare


def projection_input(root: Path, model_path: Path) -> tuple[PreparedInput, dict[str, bytes]]:
    snapshot, prefix, files, artifacts = frozen_calibration(root)
    original = load_config(*(root / f"config/{n}.json" for n in ("league", "season", "model")))
    if original.refs != snapshot.config:
        raise DataError("snapshot.config: mismatched frozen configuration")
    model, ref = load_parameters(model_path)
    if not isinstance(model, PreparationModel):
        raise ConfigError("model: annual projection requires preparation format_version 4")
    config = validate_config(
        original.league, original.season, model, original.refs.model_copy(update={"model": ref})
    )
    new_files = {
        f"config/{n}.json": files[f"{prefix}/config/{n}.json"] for n in ("league", "season")
    }
    new_files["config/model.json"] = read_bytes(model_path)
    if digest(new_files["config/model.json"]) != ref.input_sha256:
        raise ConfigError("model: changed during projection preparation")
    new_files["config/effective.json"] = canonical(config)
    zone = ZoneInfo(config.league.timezone)
    adjusted = snapshot.model_copy(
        update={
            "adjustments": ManualAdjustments(
                format_version=1,
                adjustments=tuple(
                    a.model_copy(
                        update={
                            "effective_from": a.effective_from.astimezone(zone),
                            "operation": a.operation.model_copy(
                                update={"return_at": a.operation.return_at.astimezone(zone)}
                            )
                            if isinstance(a.operation, ReturnAt)
                            else a.operation,
                        }
                    )
                    for a in snapshot.adjustments.adjustments
                ),
            )
        }
    )
    prepared = prepare(adjusted, config)
    files.update(new_files)
    entries = tuple(
        sorted(
            (*artifacts, *(artifact(p, data) for p, data in new_files.items())),
            key=lambda a: a.path,
        )
    )
    inputs = PreparedInput(
        format_version=4,
        config=config,
        artifacts=entries,
        calibration=snapshot.calibration,
        calibration_snapshot=prefix,
        players=prepared.players,
        teams=prepared.teams,
        notes=prepared.notes,
        history_pools=prepared.history_pools,
    )
    return inputs, files


def difference_report(
    current: CalculationResult, inputs: PreparedInput, previous: Path | None
) -> bytes:
    old: CalculationResult | None = None
    if previous is not None:
        old_input, old_hash = load_projection(previous / "projection-input.json")
        if old_input.config.season.season_id != inputs.config.season.season_id:
            raise DataError("previous: cannot compare different seasons")
        candidates = tuple((previous / "results").glob("calculation-*.json"))
        if len(candidates) != 1:
            raise DataError("previous: requires exactly one calculation result")
        data = read_bytes(candidates[0])
        if candidates[0].name != f"calculation-{digest(data)}.json":
            raise DataError("previous: calculation result hash mismatch")
        old = decode(CalculationResult, data, str(candidates[0]))
        if old.input_sha256 != old_hash or old.config != old_input.config.refs:
            raise DataError("previous: calculation does not match its frozen input")
    before = {p.id: p.fair for p in old.valuation.players} if old is not None else {}
    changes: list[tuple[str, float | None, float | None, float | None]] = []
    for player in current.valuation.players:
        prior = before.get(player.id)
        delta = player.fair - prior if player.fair is not None and prior is not None else None
        changes.append((player.id, prior, player.fair, delta))
    changes.sort(key=lambda p: (-abs(p[-1]) if p[-1] is not None else float("inf"), p[0]))
    return (
        json.dumps(
            {
                "format_version": 1,
                "input_sha256": current.input_sha256,
                "previous_input_sha256": old.input_sha256 if old else None,
                "largest_changes": [
                    dict(zip(("id", "before", "after", "delta"), p, strict=True))
                    for p in changes[:30]
                ],
            },
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def project(root: Path, model_path: Path, output: Path, previous: Path | None) -> Path:
    inputs, files = projection_input(root, model_path)
    try:
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".projecting-", dir=output) as folder:
            bundle = publish_bundle(
                inputs,
                inputs.artifacts,
                inputs.config.season.snapshot_as_of,
                files,
                Path(folder),
                "projection-input",
            )
            verify_calculation_input(inputs, bundle / "projection-input.json")
            result = calculate(inputs, digest(canonical(inputs)))
            publish_result(result, bundle / "results", "calculation")
            (bundle / "forecast.json").write_bytes(forecast_archive(inputs, result, bundle))
            (bundle / "difference.json").write_bytes(difference_report(result, inputs, previous))
            destination = output / bundle.name
            if destination.exists():
                raise DataError(f"{destination}: projection already exists")
            bundle.rename(destination)
        return destination
    except OSError as exc:
        raise DataError(f"{output}: projection publication failed: {exc}") from exc
