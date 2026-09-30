from datetime import date
from typing import Literal

from pydantic import AwareDatetime, JsonValue, SecretStr

from fba.contracts.base import Finite, Record, Text
from fba.contracts.data import Digest
from fba.contracts.inseason import InseasonLeague, ProjectionRules, SyncState
from fba.contracts.yahoo import DiscoveredLeague, LeagueDraft


class Sources(Record):
    forecast_path: Text
    forecast_sha256: Digest
    forecast_known_at: AwareDatetime
    player_url: Text
    permission_reference: Text


class PlayerWorkspace(Record):
    sources: Sources
    rules: ProjectionRules
    players_sha256: Digest
    priors_sha256: Digest


class LeagueState(Record):
    selected: DiscoveredLeague
    league: InseasonLeague | None
    draft: LeagueDraft | None
    confirmed_yahoo_settings: JsonValue
    confirmed_local_sha256: Digest | None
    bundle_sha256: Digest | None
    players_sha256: Digest | None
    priors_sha256: Digest | None
    normalized_sha256: Digest | None
    sources: Sources | None
    sync: SyncState


class ConnectRequest(Record):
    client_id: SecretStr
    client_secret: SecretStr


class CodeRequest(Record):
    code: SecretStr


class SelectLeagueRequest(Record):
    key: Text


class ConfirmSettingsRequest(Record):
    document: dict[str, JsonValue]
    decision: Literal["import", "keep"]


class MappingRequest(Record):
    external_id: Text
    player_id: Text


class AdjustmentChange(Record):
    player_id: Text
    field: Text
    value: Finite | Text
    starts_on: date
    ends_on: date
    reason: Text
    replaces: Text | None


class AdjustmentsRequest(Record):
    expected_sha256: Digest
    changes: tuple[AdjustmentChange, ...]
    preview: bool


class RevokeRequest(Record):
    expected_sha256: Digest
    group_id: Text


class WeekRequest(Record):
    week_id: Text


class TodayRequest(Record):
    on: date
    plan_id: Text | None


class TradeRequest(Record):
    opponent: Text
    send: tuple[Text, ...]
    receive: tuple[Text, ...]


class TradeSearchRequest(Record):
    opponent: Text | None
    size: int


class ProposalRequest(Record):
    opponent: Text
    send: tuple[Text, ...]
    receive: tuple[Text, ...]
    outcome: Literal["pending", "accepted", "rejected", "withdrawn"]
    supersedes: Text | None


class TaskCompletion(Record):
    id: Text
    completed: bool


class IgnoreFlag(Record):
    id: Text
    until: date


class UserNotes(Record):
    completed: dict[str, bool]
    ignored: dict[str, date]
    adopted: dict[str, bool]
