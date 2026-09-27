from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fba.adapters.codec import canonical, decode, digest, read_bytes
from fba.adapters.espn import validate_source_season
from fba.contracts.base import ConfigError, DataError, Record
from fba.contracts.config import (
    ConfigBundle,
    ConfigRef,
    LeagueRules,
    ModelConfig,
    SeasonConfig,
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


def load_config(league: Path, season: Path, model: Path) -> ValidatedConfig:
    rules, league_ref = load_one(league, LeagueRules, "urn:fantasy-assistant:league:1")
    try:
        ZoneInfo(rules.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"league.timezone: unknown timezone {rules.timezone}") from exc
    year, season_ref = load_one(season, SeasonConfig, "urn:fantasy-assistant:season:1")
    for source in year.sources:
        if source.adapter.startswith("espn_"):
            validate_source_season(source)
    parameters, model_ref = load_one(model, ModelConfig, "urn:fantasy-assistant:model:1")
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
