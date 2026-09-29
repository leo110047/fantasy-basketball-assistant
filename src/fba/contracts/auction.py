from typing import Literal

from fba.contracts.base import (
    Finite,
    FormatVersion,
    Natural,
    Nonnegative,
    PositiveInt,
    Record,
    Text,
)
from fba.contracts.config import (
    ConfigBundle,
    FitParameters,
    LeagueRules,
    ManagementParameters,
    PricingParameters,
)
from fba.contracts.data import Digest
from fba.contracts.projection import FrozenCalculationInput
from fba.contracts.season import ManagementInput, MarginalTask


class SolverError(RuntimeError):
    """Solver failed; no optimal result is available."""


class CalculationTimeout(SolverError):
    """The solver reached its configured deadline without proving optimality."""


class AuctionPlayer(Record):
    id: Text
    name: Text
    positions: tuple[Text, ...]
    positions_confirmed: bool
    active: bool
    projected_price: Nonnegative | None
    fair: Nonnegative | None
    utility: Finite | None


class MarginalRequest(Record):
    league: LeagueRules
    parameters: FitParameters
    management: ManagementInput
    players: tuple[AuctionPlayer, ...]
    pool: tuple[int, ...]
    tasks: tuple[MarginalTask, ...]
    pricing: PricingParameters | None
    tactics: ManagementParameters | None


class DraftTeam(Record):
    id: Text
    name: Text


class Sale(Record):
    id: Text
    player_id: Text
    buyer: Text
    amount: PositiveInt


class DraftOverride(Record):
    player_id: Text
    market: Nonnegative | None
    positions: tuple[Text, ...] | None
    reason: Text


class DraftState(Record):
    format_version: FormatVersion
    draft_id: Text
    revision: Natural
    config: ConfigBundle
    input_sha256: Digest
    mine: Text
    teams: tuple[DraftTeam, ...]
    sales: tuple[Sale, ...]
    overrides: tuple[DraftOverride, ...]
    watch: tuple[Text, ...] = ()  # Older backups contain no saved watch list.


class AuctionInput(FrozenCalculationInput):
    snapshot_sha256: Digest
    calculation_sha256: Digest
    players: tuple[AuctionPlayer, ...]
    management: ManagementInput | None


class TeamBudget(Record):
    id: Text
    owned: tuple[Text, ...]
    budget: Natural
    slots: Natural
    maximum_bid: Natural


class MarketPrice(Record):
    player_id: Text
    anchor: Nonnegative | None
    expected: Nonnegative | None
    acquisition: Nonnegative | None
    planning_cost: PositiveInt | None
    bidders: Natural


class MarketResult(Record):
    room: tuple[TeamBudget, ...]
    prices: tuple[MarketPrice, ...]
    inflation: Nonnegative


class MarketUpdate(Record):
    format_version: FormatVersion
    config: ConfigBundle
    input_sha256: Digest
    state_sha256: Digest
    market: MarketResult


class Assignment(Record):
    slot_id: Text
    player_id: Text


class Plan(Record):
    players: tuple[Text, ...]
    purchases: tuple[Text, ...]
    assignments: tuple[Assignment, ...]
    cost: Natural
    utility: Finite


class Infeasible(Record):
    reason: Text


class Cap(Record):
    player_id: Text
    amount: Natural | None
    reason: Text | None
    conditional: bool
    forced: bool
    loss: Nonnegative | None


class Nominations(Record):
    target: tuple[Text, ...]
    drain: tuple[Text, ...]
    mode: Literal["target", "drain"]


class FitStep(Record):
    step: Nonnegative
    score: Finite
    accepted: bool
    block_minimum: Finite
    block_maximum: Finite


class FitSummary(Record):
    anchor: tuple[Text, ...]
    selected_step: Nonnegative
    steps: tuple[FitStep, ...]
    samples: PositiveInt
    health_samples: PositiveInt


class FittedPlayer(Record):
    id: Text
    utility: Finite | None


class ManagedFitSummary(FitSummary):
    method: Literal["paired_managed_marginal"]
    players: tuple[FittedPlayer, ...]
    policy: PricingParameters


class AuctionResult(Record):
    format_version: FormatVersion
    algorithm: Text
    config: ConfigBundle
    input_sha256: Digest
    state_sha256: Digest
    market: MarketResult
    plan: Plan | Infeasible
    caps: tuple[Cap, ...]
    nominations: Nominations
    solver_calls: Natural
    fit: ManagedFitSummary | FitSummary | None


class Comparison(Record):
    player_id: Text
    price: PositiveInt
    buy: Plan | Infeasible
    skip: Plan | Infeasible
    delta: Finite | None
    solver_calls: Natural
