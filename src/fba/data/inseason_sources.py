import json
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from fba.contracts.archive import ForecastArchive
from fba.contracts.base import DataError
from fba.contracts.config import CalculationModel
from fba.contracts.inseason import FrozenPriors, PlayerPrior, PlayerSnapshot, ProjectionRules
from fba.contracts.yahoo import YahooCatalog
from fba.core.inseason import required_statistics
from fba.data.codec import decode, digest, read_bytes
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


def read_priors(
    path: Path, sha256: str, league: ProjectionRules, known_at: datetime
) -> FrozenPriors:
    data = read_bytes(path)
    if digest(data) != sha256:
        raise DataError(f"{path}: preseason forecast SHA-256 differs from the pinned version")
    archive = decode(ForecastArchive, data, str(path))
    if archive.config.season.season_id != league.season_id:
        raise DataError(f"{path}: forecast season differs from league")
    if not isinstance(archive.config.model, CalculationModel):
        raise DataError(f"{path}: projection model format does not contain per-game priors")
    parameters = archive.config.model.projection
    axes = (*parameters.stat_ids, parameters.threshold_stat)
    required = required_statistics(league)
    rows: list[PlayerPrior] = []
    for projected in archive.calculation.projections:
        stats = dict(zip(axes, projected.stats, strict=True))
        missing = set(required) - stats.keys()
        if missing:
            raise DataError(f"{path}.{projected.id}: missing prior statistics {sorted(missing)}")
        if projected.minutes == 0:
            if any(stats[s] > 0 for s in required):
                raise DataError(
                    f"{path}.{projected.id}: nonzero prior production with zero minutes"
                )
            continue  # Explicitly unknown rate; use the configured position/minutes peer prior.
        probabilities: dict[str, float] = {}
        for shot in league.shots:
            if shot.made not in required or stats[shot.attempted] == 0:
                continue  # A missing probability is resolved from the configured peer prior.
            probabilities[shot.id] = stats[shot.made] / stats[shot.attempted]
        rows.append(
            PlayerPrior(
                player_id=projected.id,
                minutes=projected.minutes,
                rates={s: stats[s] / projected.minutes for s in required},
                probabilities=probabilities,
            )
        )
    return FrozenPriors(
        format_version=1,
        season_id=league.season_id,
        version=str(archive.format_version),
        source_sha256=sha256,
        known_at=known_at,
        players=tuple(rows),
        distribution=parameters,
    )


def projection_rules(path: Path, catalog: YahooCatalog) -> ProjectionRules:
    archive = decode(ForecastArchive, read_bytes(path), str(path))
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
