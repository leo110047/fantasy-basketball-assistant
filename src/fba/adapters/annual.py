from datetime import datetime, time
from pathlib import Path

from fba.adapters import espn
from fba.adapters.calculation import require_completed_season
from fba.adapters.codec import canonical, decode, digest, read_bytes
from fba.adapters.config import league_zone, validate_trade_deadline
from fba.adapters.snapshots import checked_path, load_snapshot
from fba.contracts.archive import AnnualEvaluation, ForecastArchive
from fba.contracts.base import DataError
from fba.contracts.config import PreparationModel, Source, ValidatedConfig
from fba.contracts.projection import (
    CalculationResult,
    EvaluationInput,
    PredictionVariant,
    PreparedInput,
    Projected,
)
from fba.core.actual import observed_players
from fba.core.config import validate_config
from fba.core.evaluation import evaluate


def forecast_archive(inputs: PreparedInput, result: CalculationResult, root: Path) -> bytes:
    snapshot = load_snapshot(checked_path(root, inputs.calibration_snapshot))
    lengths = {t.full_season_games for t in inputs.teams}
    if len(lengths) != 1:
        raise DataError("annual.forecast: evaluation requires a common full-season games count")
    return canonical(
        ForecastArchive(
            format_version=1,
            config=inputs.config,
            season_games=next(iter(lengths)),
            calculation=result,
            identities=tuple(
                sorted(
                    (i for p in snapshot.players for i in p.identities),
                    key=lambda i: (i.player_id, i.provider, i.provider_player_id),
                )
            ),
        )
    )


def validate_archive(archive: ForecastArchive, current: ValidatedConfig) -> None:
    config = archive.config
    league_zone(config.league.timezone)
    validate_trade_deadline(config.league, config.season)
    validate_config(config.league, config.season, config.model, config.refs)
    for name in ("league", "season", "model"):
        if digest(canonical(getattr(config, name))) != getattr(config.refs, name).effective_sha256:
            raise DataError(f"annual.forecast.config.{name}: effective hash mismatch")
    if archive.calculation.config != config.refs:
        raise DataError("annual.forecast: calculation configuration mismatch")
    if config.season.season_id != current.season.previous_season_id:
        raise DataError("annual.forecast: requires the configured previous season")
    require_completed_season(config, current.season.snapshot_as_of)


def archive_source(config: ValidatedConfig) -> Source:
    sources = tuple(s for s in config.season.sources if s.role == "forecast_archive")
    if len(sources) != 1 or sources[0].adapter != "fba_forecast":
        raise DataError(
            "annual: configure exactly one forecast_archive source with adapter fba_forecast"
        )
    return sources[0]


def previous_evaluation(snapshot_root: Path, config: ValidatedConfig) -> bytes:
    source = archive_source(config)
    history = next(s for s in config.season.sources if s.role == "game_logs")
    if history.adapter != "espn_players":
        raise DataError("annual.actual: unsupported game_logs adapter")
    snapshot = load_snapshot(snapshot_root)
    if snapshot.config != config.refs:
        raise DataError("annual: snapshot configuration mismatch")
    artifacts = {a.path: a for a in snapshot.artifacts}
    for item in (source, history):
        entry = artifacts.get(f"raw/{item.id}.json")
        if entry is None or entry.provenance is None or entry.provenance.source_id != item.id:
            raise DataError(f"annual.{item.id}: source must be frozen with provenance")
    archive_data = read_bytes(snapshot_root / f"raw/{source.id}.json")
    archive = decode(ForecastArchive, archive_data, source.id)
    validate_archive(archive, config)
    actual_data = read_bytes(snapshot_root / f"raw/{history.id}.json")
    players = espn.decode_players(actual_data, history.id)
    season = int(history.season_code)
    actual = observed_players(
        espn.game_logs(players, season, history.id),
        espn.season_totals(players, season, history.id),
        archive.config,
    )
    predicted = provider_predictions(archive)
    if not {p.id for p in predicted} <= {str(p.id) for p in players}:
        raise DataError("annual.actual: forecast player absent from the source catalogue")
    model = archive.config.model
    if not isinstance(model, PreparationModel):
        raise DataError("annual.forecast: preparation model required")
    inputs = EvaluationInput(
        format_version=1,
        config=archive.config,
        artifacts=(),
        stat_ids=(*model.projection.stat_ids, model.projection.threshold_stat),
        season_games=archive.season_games,
        actual=actual,
        predictions=(PredictionVariant(id="archived", players=predicted),),
        common_ids=tuple(p.id for p in predicted),
    )
    result = AnnualEvaluation(
        format_version=1,
        snapshot_sha256=digest(read_bytes(snapshot_root / "snapshot.json")),
        config=config.refs,
        forecast_sha256=digest(archive_data),
        actual_sha256=digest(actual_data),
        information_mode="published"
        if archive.config.season.snapshot_as_of
        < datetime.combine(
            archive.config.season.starts_on, time.min, league_zone(archive.config.league.timezone)
        )
        else "retrospective",
        evaluation=evaluate(inputs, digest(canonical(inputs))),
    )
    return canonical(result)


def provider_predictions(archive: ForecastArchive) -> tuple[Projected, ...]:
    identities = tuple(i for i in archive.identities if i.provider == "espn")
    by_id = {i.player_id: i.provider_player_id for i in identities}
    if len(by_id) != len(identities) or len(set(by_id.values())) != len(identities):
        raise DataError("annual.forecast.identities: ambiguous provider IDs")
    predicted = archive.calculation.projections
    if any(p.id not in by_id for p in predicted):
        raise DataError("annual.forecast.identities: missing explicit provider ID")
    return tuple(
        sorted((p.model_copy(update={"id": by_id[p.id]}) for p in predicted), key=lambda p: p.id)
    )
