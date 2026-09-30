from typing import Literal

from fba.contracts.auction import (
    AnnotatedAuctionDetail,
    AuctionDetail,
    AuctionPlayer,
    AuctionResult,
    CapSensitivity,
    Comparison,
    DraftState,
    ForecastScenario,
    MarketUpdate,
)
from fba.contracts.base import FormatVersion, Natural, PositiveInt, Record, Text
from fba.contracts.config import LeagueRules
from fba.contracts.data import Digest, TeamLabel
from fba.contracts.formula import FormulaDefinition
from fba.contracts.streaming import StreamingSummary


class SaveDraft(Record):
    expected_sha256: Digest
    draft: DraftState


class StateRequest(Record):
    state_sha256: Digest


class StreamingRequest(StateRequest):
    mode: Literal["equal", "fit"]


class StreamingResult(Record):
    state_sha256: Digest
    mode: Literal["equal", "fit"]
    streaming: StreamingSummary


class CompareRequest(StateRequest):
    mode: Literal["equal", "fit"]
    player_id: Text
    price: PositiveInt


class SensitivityRequest(StateRequest):
    player_id: Text


class SensitivityResult(Record):
    state_sha256: Digest
    sensitivity: CapSensitivity


class JobView(Record):
    status: Literal["updating", "ready", "failed"]
    result: AuctionResult | None
    error: Text | None
    elapsed_ns: Natural | None = None


class DeskState(Record):
    format_version: FormatVersion
    state: DraftState
    market: MarketUpdate


class DeskResults(Record):
    state_sha256: Digest
    equal: JobView
    fit: JobView


class DeskBootstrap(Record):
    formulas: tuple[FormulaDefinition, ...]
    format_version: FormatVersion
    season_id: Text
    snapshot_sha256: Digest
    league: LeagueRules
    players: tuple[AuctionPlayer, ...]
    details: tuple[AnnotatedAuctionDetail | AuctionDetail, ...] | None
    teams: tuple[TeamLabel, ...] | None
    scenarios: tuple[ForecastScenario, ...] | None
    streaming_candidates: tuple[Natural, ...] | None
    desk: DeskState


class Compared(Record):
    mode: Literal["equal", "fit"]
    state_sha256: Digest
    comparison: Comparison


class DeskError(Record):
    error: Text


class SaveUnconfirmed(RuntimeError):
    """The draft was replaced, but durability or execution logging could not be confirmed."""


class DeskHealth(Record):
    status: Literal["ok"]


class DeskExecution(Record):
    format_version: FormatVersion
    stage: Literal["market", "equal", "fit", "compare", "sensitivity", "streaming"]
    state: DraftState
    state_sha256: Digest
    elapsed_ns: Natural
    solver_calls: Natural | None
    result: (
        MarketUpdate | AuctionResult | Compared | SensitivityResult | StreamingResult | DeskError
    )
