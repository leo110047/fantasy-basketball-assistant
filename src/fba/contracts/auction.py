from typing import Annotated, Literal, NamedTuple, Self

from pydantic import Field, model_validator

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
    ConfigRef,
    FitParameters,
    LeagueRules,
    ManagementParameters,
    PricingParameters,
)
from fba.contracts.data import Adjustment, Digest, Provenance, StatValue, TeamLabel
from fba.contracts.formula import ArrayFormulaTrace, FormulaTrace
from fba.contracts.projection import CategoryScore, FrozenCalculationInput
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


class AuctionDetail(Record):
    traces: tuple[FormulaTrace, ...] = ()  # Legacy immutable forecast compatibility.
    player_id: Text
    team_id: Text | None
    expected_games: Nonnegative | None
    minutes: Nonnegative | None
    stats: tuple[StatValue, ...]
    average_price: Nonnegative | None
    forecast_sources: tuple[Text, ...]
    forecast_usable: bool
    preparation_warnings: tuple[Text, ...]
    forecast_provenance: tuple[Provenance, ...]
    adjustments: tuple[Adjustment, ...]
    categories: tuple[CategoryScore, ...]


class AnnotatedAuctionDetail(AuctionDetail):
    history_games: Natural
    history_minimum: PositiveInt
    original_expected_games: Nonnegative | None
    healthy_games_threshold: Nonnegative


class ScenarioPrice(Record):
    player_id: Text
    fair: Nonnegative | None


class ForecastScenario(Record):
    name: Text
    model: ConfigRef
    projection_sha256: Digest
    calculation_sha256: Digest
    prices: tuple[ScenarioPrice, ...]


class AuctionInput(FrozenCalculationInput):
    format_version: Annotated[int, Field(ge=1, le=4)]
    snapshot_sha256: Digest
    calculation_sha256: Digest
    players: tuple[AuctionPlayer, ...]
    management: ManagementInput | None
    details: tuple[AnnotatedAuctionDetail | AuctionDetail, ...] | None = None
    teams: tuple[TeamLabel, ...] | None = None
    scenarios: tuple[ForecastScenario, ...] | None = None

    @model_validator(mode="after")
    def detail_version(self) -> Self:
        if (self.format_version >= 2) != (self.details is not None):
            raise ValueError("auction.details: required in format_version 2 or later")
        if (self.format_version >= 3) != (self.teams is not None):
            raise ValueError("auction.teams: required in format_version 3 or later")
        if (self.format_version == 4) != (self.scenarios is not None):
            raise ValueError("auction.scenarios: required only in format_version 4")
        if any(
            isinstance(detail, AnnotatedAuctionDetail) != (self.format_version >= 3)
            for detail in self.details or ()
        ):
            raise ValueError("auction.details: annotations require format_version 3 or later")
        return self


class TeamBudget(Record):
    id: Text
    owned: tuple[Text, ...]
    budget: Natural
    slots: Natural
    maximum_bid: Natural


class MarketPrice(Record):
    traces: tuple[FormulaTrace, ...] = ()
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
    traces: tuple[FormulaTrace, ...] = ()
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


class FitCategory(Record):
    id: Text
    lead_share: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    marginal_weight: Nonnegative


class FitDiagnostics(Record):
    categories: tuple[FitCategory, ...]
    baseline_score: Finite
    selected_score: Finite
    compared_roster: tuple[Text, ...]
    opponents: tuple[tuple[Text, ...], ...]
    traces: tuple[FormulaTrace | ArrayFormulaTrace, ...] = ()


class FitSummary(Record):
    anchor: tuple[Text, ...]
    selected_step: Nonnegative
    steps: tuple[FitStep, ...]
    samples: PositiveInt
    health_samples: PositiveInt
    diagnostics: FitDiagnostics | None = None


class FittedPlayer(Record):
    id: Text
    utility: Finite | None


class ManagedFitSummary(FitSummary):
    method: Literal["paired_managed_marginal"]
    players: tuple[FittedPlayer, ...]
    policy: PricingParameters
    sensitivity: tuple[tuple[FittedPlayer, ...], ...] | None = None


class CapSensitivity(Record):
    player_id: Text
    status: Literal["ready", "baseline_retained"]
    central: Natural
    groups: tuple[Natural, ...]
    low: Natural | None
    high: Natural | None
    solver_calls: Natural


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


class CapCalculation(NamedTuple):
    amount: int
    loss: float | None
    forced: bool
    traces: tuple[FormulaTrace, ...]
