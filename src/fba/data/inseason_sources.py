import json
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from fba.contracts.archive import ForecastArchive
from fba.contracts.base import DataError
from fba.contracts.inseason import InseasonForecast, PlayerSnapshot, ProjectionRules
from fba.contracts.yahoo import YahooCatalog
from fba.data.codec import checked_json, decode, read_bytes
from fba.data.yahoo_auth import Transport, http_request


class PlayerSource(Protocol):
    def fetch(self) -> PlayerSnapshot: ...


class AuthorizedFeed:
    """Adapter for an explicitly licensed provider's normalized JSON feed."""

    def __init__(
        self,
        url: str,
        permission_reference: str,
        timeout: float,
        transport: Transport = http_request,
    ) -> None:
        if urlsplit(url).scheme != "https" or not permission_reference.strip():
            raise DataError(
                "player_source: HTTPS URL and provider permission reference are required"
            )
        self.url, self.permission_reference = url, permission_reference
        self.timeout, self.transport = timeout, transport

    def fetch(self) -> PlayerSnapshot:
        result = self.transport(self.url, "GET", {"Accept": "application/json"}, None, self.timeout)
        if result.status != 200:
            raise DataError(f"player_source: HTTP {result.status}")
        return decode(PlayerSnapshot, result.body, "player_source")


def forecast_document(data: bytes, label: str) -> ForecastArchive | InseasonForecast:
    adapter: TypeAdapter[ForecastArchive | InseasonForecast] = TypeAdapter(
        ForecastArchive | InseasonForecast
    )
    try:
        return adapter.validate_json(checked_json(data, label))
    except ValidationError as exc:
        raise DataError(f"{label}: {exc}") from exc


def projection_rules(path: Path, catalog: YahooCatalog) -> ProjectionRules:
    archive = forecast_document(read_bytes(path), str(path))
    defaults = catalog.confirmation_defaults
    return ProjectionRules.model_validate_json(
        json.dumps(
            {
                "season_id": archive.config.season.season_id,
                "starts_on": str(archive.config.season.starts_on),
                "ends_on": str(archive.config.season.ends_on),
                "timezone": archive.config.league.timezone,
                "categories": [c.model_dump(mode="json") for c in archive.config.league.categories],
                "base_stats": list(catalog.base_stats),
                "shots": [s.model_dump(mode="json") for s in catalog.shots],
                "derived": [s.model_dump(mode="json") for s in catalog.derived],
                "players_on_court": defaults["players_on_court"],
                "regulation_minutes": defaults["regulation_minutes"],
            }
        )
    )
