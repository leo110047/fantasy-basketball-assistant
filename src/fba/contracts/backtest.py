from datetime import date
from typing import Literal

from pydantic import AwareDatetime

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
from fba.contracts.data import Digest
from fba.contracts.projection import FrozenCalculationInput
from fba.contracts.season import SeasonEvent


class ReplayTeam(Record):
    id: Text
    seed: PositiveInt
    roster: tuple[Text, ...]
    streaming_slots: Natural


class HealthObservation(Record):
    player_id: Text
    published_at: AwareDatetime
    available: bool
    status: Text
    source_artifact: Text


class ActualBox(Record):
    player_id: Text
    day: date
    stats: tuple[Nonnegative, ...]
    source_artifact: Text


class HealthArchive(Record):
    format_version: FormatVersion
    observations: tuple[HealthObservation, ...]


class ActualArchive(Record):
    format_version: FormatVersion
    boxes: tuple[ActualBox, ...]


class Pairing(Record):
    week_id: Text
    home: Text
    away: Text | None


class ReplayInput(FrozenCalculationInput):
    auction_path: Text
    auction_sha256: Digest
    evaluated_at: AwareDatetime
    information_mode: Literal["published", "retrospective"]
    decision_times: tuple[AwareDatetime, ...]
    teams: tuple[ReplayTeam, ...]
    free_agents: tuple[Text, ...]
    upgrades: bool
    health: tuple[HealthObservation, ...]
    actual: tuple[ActualBox, ...]
    pairings: tuple[Pairing, ...]


class CategoryOutcome(Record):
    id: Text
    home: Finite
    away: Finite
    winner: Literal["home", "away", "tie"]


class WeekOutcome(Record):
    week_id: Text
    home: Text
    away: Text | None
    categories: tuple[CategoryOutcome, ...]
    home_points: Nonnegative
    away_points: Nonnegative
    winner: Text | None


class Standing(Record):
    team_id: Text
    wins: Nonnegative
    losses: Natural
    ties: Natural
    category_points: Nonnegative
    seed: PositiveInt


class ReplayResult(Record):
    format_version: FormatVersion
    algorithm: Text
    config: ConfigBundle
    input_sha256: Digest
    auction_sha256: Digest
    information_mode: Literal["published", "retrospective"]
    player_ids: tuple[Text, ...]
    team_ids: tuple[Text, ...]
    days: tuple[date, ...]
    events: tuple[SeasonEvent, ...]
    weekly_boxes: tuple[tuple[tuple[Nonnegative, ...], ...], ...]
    adds: tuple[tuple[tuple[Natural, ...], ...], ...]
    outcomes: tuple[WeekOutcome, ...]
    standings: tuple[Standing, ...]
    champion: Text
