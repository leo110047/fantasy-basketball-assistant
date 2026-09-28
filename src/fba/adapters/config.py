from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from fba.adapters.codec import canonical, checked_json, decode, digest, read_bytes
from fba.adapters.espn import validate_source_season
from fba.contracts.base import ConfigError, DataError, Record
from fba.contracts.config import (
    AuctionModel,
    CalculationModel,
    ConfigBundle,
    ConfigRef,
    LeagueRules,
    ModelConfig,
    ModelDocument,
    PreparationModel,
    ResourceModel,
    SeasonConfig,
    SeasonModel,
    ValidatedConfig,
)
from fba.core.config import validate_config


def load_one[T: Record](path: Path, model: type[T], schema: str) -> tuple[T, ConfigRef]:
    try:
        data = read_bytes(path)
        parsed = decode(model, data, str(path))
    except DataError as exc:
        raise ConfigError(str(exc)) from exc
    return parsed, ConfigRef(
        schema_id=schema,
        input_sha256=digest(data),
        effective_sha256=digest(canonical(parsed)),
    )


def league_zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"league.timezone: unknown timezone {timezone}") from exc


def validate_trade_deadline(rules: LeagueRules, season: SeasonConfig) -> None:
    deadline = rules.trade_deadline
    if deadline is not None:
        local_day = deadline.astimezone(league_zone(rules.timezone)).date()
        if not season.starts_on <= local_day <= season.ends_on:
            raise ConfigError(
                "league.trade_deadline: outside the configured season in league.timezone"
            )


def load_config(league: Path, season: Path, model: Path) -> ValidatedConfig:
    rules, league_ref = load_one(league, LeagueRules, "urn:fantasy-assistant:league:1")
    league_zone(rules.timezone)
    year, season_ref = load_one(season, SeasonConfig, "urn:fantasy-assistant:season:1")
    validate_trade_deadline(rules, year)
    for source in year.sources:
        if source.adapter.startswith("espn_"):
            validate_source_season(source)
    parameters, model_ref = load_parameters(model)
    return validate_config(
        rules,
        year,
        parameters,
        ConfigBundle(
            league=league_ref,
            season=season_ref,
            model=model_ref,
        ),
    )


def load_parameters(
    path: Path,
) -> tuple[
    ModelConfig | ResourceModel | CalculationModel | PreparationModel | AuctionModel | SeasonModel,
    ConfigRef,
]:
    data = read_bytes(path)
    try:
        parameters = ModelDocument.model_validate_json(checked_json(data, str(path))).root
    except (ValidationError, DataError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parameters, ConfigRef(
        schema_id=f"urn:fantasy-assistant:model:{parameters.format_version}",
        input_sha256=digest(data),
        effective_sha256=digest(canonical(parameters)),
    )
