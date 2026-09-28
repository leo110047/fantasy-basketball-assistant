from typing import Literal

from fba.contracts.auction import (
    AuctionPlayer,
    AuctionResult,
    Comparison,
    DraftState,
    MarketUpdate,
)
from fba.contracts.base import FormatVersion, Natural, PositiveInt, Record, Text
from fba.contracts.config import LeagueRules
from fba.contracts.data import Digest


class SaveDraft(Record):
    expected_sha256: Digest
    draft: DraftState


class StateRequest(Record):
    state_sha256: Digest


class CompareRequest(StateRequest):
    player_id: Text
    price: PositiveInt


class JobView(Record):
    status: Literal["updating", "ready", "failed"]
    result: AuctionResult | None
    error: Text | None


class DeskState(Record):
    format_version: FormatVersion
    state: DraftState
    market: MarketUpdate


class DeskResults(Record):
    state_sha256: Digest
    equal: JobView
    fit: JobView


class DeskBootstrap(Record):
    format_version: FormatVersion
    season_id: Text
    snapshot_sha256: Digest
    league: LeagueRules
    players: tuple[AuctionPlayer, ...]
    desk: DeskState


class Compared(Record):
    state_sha256: Digest
    comparison: Comparison


class DeskError(Record):
    error: Text


class DeskExecution(Record):
    format_version: FormatVersion
    stage: Literal["market", "equal", "fit", "compare"]
    state: DraftState
    state_sha256: Digest
    elapsed_ns: Natural
    solver_calls: Natural | None
    result: MarketUpdate | AuctionResult | Compared | DeskError
