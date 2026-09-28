from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from fba.contracts.base import (
    Finite,
    FormatVersion,
    Natural,
    Nonnegative,
    PositiveInt,
    Record,
    Text,
)
from fba.contracts.config import ConfigBundle

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Provenance(Record):
    source_id: Text
    url: Text
    raw_sha256: Digest
    available_as_of: AwareDatetime
    retrieved_at: AwareDatetime
    delivery: Literal["fetch", "manual"]


class Identity(Record):
    player_id: Text
    provider: Text
    provider_player_id: Text
    method: Literal["exported_id", "explicit_mapping"]
    source: Text
    confirmed_at: AwareDatetime


class IdentityMap(Record):
    format_version: FormatVersion
    entries: tuple[Identity, ...]


class Multiply(Record):
    kind: Literal["multiply"]
    stat_id: Text
    factor: Nonnegative


class ExpectedGames(Record):
    kind: Literal["expected_games"]
    games: Nonnegative


class ReturnAt(Record):
    kind: Literal["return_at"]
    return_at: AwareDatetime


class Adjustment(Record):
    id: Text
    player_id: Text
    published_at: AwareDatetime
    effective_from: AwareDatetime
    reason: Text
    source: Text
    assumption: bool
    operation: Annotated[Multiply | ExpectedGames | ReturnAt, Field(discriminator="kind")]


class ManualAdjustments(Record):
    format_version: FormatVersion
    adjustments: tuple[Adjustment, ...]


class RosterRow(Record):
    id: Text
    name: Text
    positions: Annotated[tuple[Text, ...], Field(min_length=1)]
    rank: PositiveInt
    projected_price: Nonnegative | None
    average_price: Nonnegative | None


class StatValue(Record):
    id: Text
    value: Nonnegative | None
    missing_reason: Text | None


class ProviderPlayer(Record):
    provider: Text
    id: Text
    name: Text
    team_id: Text | None


class Forecast(Record):
    player_id: Text
    expected_games: Nonnegative
    totals: tuple[StatValue, ...]
    source_id: Text


class PlayerGame(Record):
    player_id: Text
    game_id: Text
    team_id: Text
    stats: tuple[StatValue, ...]
    source_id: Text


class Game(Record):
    id: Text
    home_team_id: Text
    away_team_id: Text
    tipoff: AwareDatetime
    local_date: date
    source_id: Text
    status: Literal["scheduled", "completed", "postponed", "cancelled"]


class ScheduleCount(Record):
    team_id: Text
    announced: Natural
    pending: Natural
    pending_reason: Text | None
    source_id: Text


class Player(Record):
    roster: RosterRow
    identities: tuple[Identity, ...]
    team_id: Text | None
    history_status: Literal["available", "no_previous_season_history", "incomplete"]


class ActualGames(Record):
    player_id: Text
    games: Natural


class ActualSeason(ActualGames):
    totals: tuple[StatValue, ...]


class CalibrationPair(Record):
    player_id: Text
    projected_games: Nonnegative
    actual_games: Natural


class Calibration(Record):
    training_season_id: Text
    method: Literal["ordinary_least_squares"]
    intercept: Finite
    slope: Finite
    sample_size: PositiveInt
    inputs_sha256: tuple[Digest, ...]


class Artifact(Record):
    path: Text
    sha256: Digest
    size: Natural
    provenance: Provenance | None


class Snapshot(Record):
    format_version: FormatVersion
    season_id: Text
    version: PositiveInt
    as_of: AwareDatetime
    config: ConfigBundle
    artifacts: tuple[Artifact, ...]
    players: tuple[Player, ...]
    schedule: tuple[Game, ...]
    schedule_counts: tuple[ScheduleCount, ...]
    forecasts: tuple[Forecast, ...]
    history: tuple[PlayerGame, ...]
    calibration: Calibration
    adjustments: ManualAdjustments
