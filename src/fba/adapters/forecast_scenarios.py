from pathlib import Path

from fba.adapters.calculation import decode_projection, load_projection
from fba.adapters.config import load_parameters
from fba.adapters.snapshots import checked_path
from fba.contracts.auction import AuctionInput, AuctionPlayer, ForecastScenario, ScenarioPrice
from fba.contracts.base import DataError
from fba.contracts.config import ConfigBundle, ValidatedConfig
from fba.contracts.projection import CalculationResult, ProductionInput
from fba.core.config import validate_config
from fba.data.codec import decode, digest, read_bytes


def valued_projection(root: Path) -> tuple[ProductionInput, str, CalculationResult, bytes]:
    inputs, input_hash = load_projection(root / "projection-input.json")
    paths = tuple((root / "results").glob("calculation-*.json"))
    if len(paths) != 1:
        raise DataError("auction.projection: requires exactly one calculation result")
    payload = read_bytes(paths[0])
    result = decode(CalculationResult, payload, str(paths[0]))
    if (
        paths[0].name != f"calculation-{digest(payload)}.json"
        or result.input_sha256 != input_hash
        or result.config != inputs.config.refs
    ):
        raise DataError("auction.projection: calculation hash or input linkage mismatch")
    return inputs, input_hash, result, payload


def scenario_prices(
    result: CalculationResult, players: tuple[AuctionPlayer, ...]
) -> tuple[ScenarioPrice, ...]:
    values = {p.id: p.fair for p in result.valuation.players}
    if len(values) != len(result.valuation.players) or set(values) != {p.id for p in players}:
        raise DataError("auction.scenarios: player populations disagree")
    return tuple(ScenarioPrice(player_id=p, fair=values[p]) for p in sorted(values))


def scenario_paths(row: ForecastScenario) -> tuple[str, str, str]:
    return (
        f"scenarios/calculation-{row.calculation_sha256}.json",
        f"scenarios/model-{row.model.input_sha256}.json",
        f"scenarios/projection-{row.projection_sha256}.json",
    )


def freeze_scenarios(
    sources: tuple[tuple[str, Path], ...],
    config: ValidatedConfig,
    snapshot_hash: str,
    players: tuple[AuctionPlayer, ...],
) -> tuple[tuple[ForecastScenario, ...], dict[str, bytes]]:
    names = tuple(name for name, _ in sources)
    if any(not n.strip() for n in names) or len(set(names)) != len(names):
        raise DataError("auction.scenarios: names must be nonblank and unique")
    rows: list[ForecastScenario] = []
    files: dict[str, bytes] = {}
    for name, root in sorted(sources):
        inputs, input_hash, result, payload = valued_projection(root)
        snapshot = read_bytes(checked_path(root, inputs.calibration_snapshot) / "snapshot.json")
        if (
            inputs.config.league != config.league
            or inputs.config.season != config.season
            or inputs.config.refs.league != config.refs.league
            or inputs.config.refs.season != config.refs.season
            or digest(snapshot) != snapshot_hash
        ):
            raise DataError("auction.scenarios: require the same league, season and snapshot")
        row = ForecastScenario(
            name=name,
            model=inputs.config.refs.model,
            projection_sha256=input_hash,
            calculation_sha256=digest(payload),
            prices=scenario_prices(result, players),
        )
        result_path, model_path, projection_path = scenario_paths(row)
        model = read_bytes(root / "config/model.json")
        projection = read_bytes(root / "projection-input.json")
        if digest(model) != row.model.input_sha256:
            raise DataError("auction.scenarios: model changed during preparation")
        if digest(projection) != input_hash:
            raise DataError("auction.scenarios: projection changed during preparation")
        files[result_path], files[model_path] = payload, model
        files[projection_path] = projection
        rows.append(row)
    return tuple(rows), files


def verify_scenarios(inputs: AuctionInput, root: Path) -> None:
    scenarios = inputs.scenarios or ()
    names = tuple(s.name for s in scenarios)
    if any(not n.strip() for n in names) or names != tuple(sorted(set(names))):
        raise DataError("auction.scenarios: require unique, sorted scenario names")
    artifacts = {a.path: a.sha256 for a in inputs.artifacts}
    for row in scenarios:
        result_path, model_path, projection_path = scenario_paths(row)
        if (
            artifacts.get(result_path) != row.calculation_sha256
            or artifacts.get(model_path) != row.model.input_sha256
            or artifacts.get(projection_path) != row.projection_sha256
        ):
            raise DataError("auction.scenarios: missing or mismatched frozen artifact")
        result = decode(CalculationResult, read_bytes(checked_path(root, result_path)), result_path)
        model, ref = load_parameters(checked_path(root, model_path))
        refs = ConfigBundle(
            league=inputs.config.refs.league, season=inputs.config.refs.season, model=ref
        )
        config = validate_config(inputs.config.league, inputs.config.season, model, refs)
        projection = decode_projection(
            read_bytes(checked_path(root, projection_path)), projection_path
        )
        snapshots = {a.path: a.sha256 for a in projection.artifacts}
        if (
            snapshots.get(f"{projection.calibration_snapshot}/snapshot.json")
            != inputs.snapshot_sha256
        ):
            raise DataError("auction.scenarios: frozen projection uses a different snapshot")
        if (
            row.model != ref
            or projection.config != config
            or result.config != refs
            or result.input_sha256 != row.projection_sha256
            or row.prices != scenario_prices(result, inputs.players)
        ):
            raise DataError("auction.scenarios: values or lineage differ from frozen calculation")
