from datetime import date, time
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, RootModel

from fba.contracts.base import (
    Finite,
    FormatVersion,
    Natural,
    Nonnegative,
    PositiveInt,
    Record,
    Text,
)


class Evidence(Record):
    kind: Literal["Assumption", "source", "backtest"]
    reference: Text
    as_of: AwareDatetime
    reason: Text


class Assumption(Record):
    field_path: Text
    evidence: Evidence


class Term(Record):
    stat_id: Text
    coefficient: Finite


class Linear(Record):
    kind: Literal["linear"]
    terms: Annotated[tuple[Term, ...], Field(min_length=1)]


class Ratio(Record):
    kind: Literal["ratio"]
    numerator: Annotated[tuple[Term, ...], Field(min_length=1)]
    denominator: Annotated[tuple[Term, ...], Field(min_length=1)]
    zero_denominator: Literal["error", "zero", "numerator"]


class Category(Record):
    id: Text
    label: Text
    formula: Annotated[Linear | Ratio, Field(discriminator="kind")]
    direction: Literal["higher", "lower"]
    comparison_decimals: Annotated[int, Field(ge=0, le=12)]
    tie_value: Annotated[float, Field(ge=0, le=1)]


class StarterSlot(Record):
    id: Text
    label: Text
    eligible_positions: Annotated[tuple[Text, ...], Field(min_length=1)]


class InjurySlot(Record):
    id: Text
    label: Text
    count: PositiveInt
    eligible_statuses: Annotated[tuple[Text, ...], Field(min_length=1)]


class Period(Record):
    id: Text
    start: date
    end: date


class Matchup(Period):
    phase: Literal["regular", "playoff", "postseason"]


class Scoring(Record):
    mode: Literal["h2h_one_win"]
    week_tie: Literal["tie", "half_win", "loss"]
    category_ties: Literal["exclude", "use_tie_value"]


class Transactions(Record):
    adds_per_period: Natural
    add_periods: Annotated[tuple[Period, ...], Field(min_length=1)]
    effective: Literal["same_day", "next_day"]
    cutoff_local_time: time
    waiver_days: Natural


class Lineup(Record):
    lock_mode: Literal["daily", "weekly"]
    lock_at: Literal["period_start", "first_game", "player_game"]
    lock_local_time: time


class Playoffs(Record):
    team_count: PositiveInt
    week_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    seeding: Annotated[
        tuple[Literal["record", "head_to_head", "category_record", "seed"], ...],
        Field(min_length=1),
    ]
    byes: Natural
    reseed: bool
    matchup_tie: Literal["higher_seed", "category_record"]


class LeagueRules(Record):
    format_version: FormatVersion
    league_id: Text
    teams: Annotated[int, Field(ge=2)]
    budget: PositiveInt
    minimum_bid: PositiveInt
    bid_increment: PositiveInt
    timezone: Text
    positions: Annotated[tuple[Text, ...], Field(min_length=1)]
    starter_slots: Annotated[tuple[StarterSlot, ...], Field(min_length=1)]
    bench_slots: Natural
    injury_slots: tuple[InjurySlot, ...]
    categories: Annotated[tuple[Category, ...], Field(min_length=1)]
    scoring: Scoring
    transactions: Transactions
    lineup: Lineup
    matchups: Annotated[tuple[Matchup, ...], Field(min_length=1)]
    playoffs: Playoffs
    trade_deadline: AwareDatetime | None
    assumptions: tuple[Assumption, ...]


class Columns(Record):
    player_id: Text
    name: Text
    positions: Text
    rank: Text
    projected_price: Text
    average_price: Text


class RosterImport(Record):
    path: Text
    format: Literal["csv", "yahoo_export"]
    encoding: Text
    delimiter: Annotated[str, Field(min_length=1, max_length=1)]
    columns: Columns


class ManualCapture(Record):
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    retrieved_at: AwareDatetime


class Source(Record):
    id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")]
    role: Literal[
        "projections",
        "game_logs",
        "schedule",
        "rosters",
        "official_schedule_counts",
        "historical_projections",
        "forecast_archive",
    ]
    adapter: Text
    url: Text
    season_code: Text
    season_id: Text
    available_as_of: AwareDatetime
    delivery: Literal["fetch", "manual"]
    manual_file: Text | None
    manual_capture: ManualCapture | None


class Observed(Record):
    kind: Literal["observed"]


class ThresholdCount(Record):
    kind: Literal["threshold_count"]
    stat_ids: Annotated[tuple[Text, ...], Field(min_length=1)]
    threshold: Nonnegative
    minimum_hits: PositiveInt


class StatDefinition(Record):
    id: Text
    label: Text
    unit: Literal["count", "minutes", "games"]
    definition: Annotated[Observed | Linear | ThresholdCount, Field(discriminator="kind")]


class SeasonConfig(Record):
    format_version: FormatVersion
    season_id: Text
    starts_on: date
    ends_on: date
    snapshot_as_of: AwareDatetime
    previous_season_id: Text
    roster_import: RosterImport
    identity_map_path: Text
    manual_adjustments_path: Text
    sources: Annotated[tuple[Source, ...], Field(min_length=1)]
    stat_definitions: Annotated[tuple[StatDefinition, ...], Field(min_length=1)]
    assumptions: tuple[Assumption, ...]


class CalibrationMethod(Record):
    value: Literal["ordinary_least_squares"]
    evidence: Evidence


class ModelConfig(Record):
    """Stage 1 accepts only settings consumed by snapshot construction."""

    format_version: FormatVersion
    calibration: CalibrationMethod


class NestedCount(Record):
    child: Text
    parent: Text


class DistributionParameters(Record):
    stat_ids: tuple[Text, ...]
    rounding_groups: tuple[tuple[Text, ...], ...]
    nested_counts: tuple[NestedCount, ...]
    scoring_stat: Text
    scoring_terms: tuple[Term, ...]
    threshold_stat: Text
    count_pseudocount: Nonnegative
    attempt_pseudocount: Nonnegative
    search_iterations: PositiveInt
    integration_batch_size: PositiveInt
    feasibility_tolerance: Nonnegative
    evidence: Evidence


class ResourceParameters(DistributionParameters):
    offense_stats: tuple[Text, ...]
    possession_terms: tuple[Term, ...]
    second_chance_stat: Text
    assist_stat: Text
    made_stat: Text
    regulation_minutes: PositiveInt
    players_on_court: PositiveInt
    minimum_cost_scale: Nonnegative
    minimum_usage_scale: Nonnegative


class PriorWeight(Record):
    id: Text
    weight: Nonnegative


class ProjectionParameters(DistributionParameters):
    prior_weights: tuple[PriorWeight, ...]


class ValuationParameters(Record):
    pool_iterations: PositiveInt
    healthy_games: Nonnegative
    replacement_count: PositiveInt
    result_decimals: Annotated[int, Field(ge=0, le=12)]
    evidence: Evidence


class ResourceModel(Record):
    format_version: Annotated[int, Field(ge=2, le=2)]
    calibration: CalibrationMethod
    projection: ResourceParameters
    valuation: ValuationParameters


class CalculationModel(Record):
    format_version: Annotated[int, Field(ge=3, le=3)]
    calibration: CalibrationMethod
    projection: ProjectionParameters
    valuation: ValuationParameters


class PositionPool(Record):
    id: Text
    any_positions: tuple[Text, ...]
    exact_positions: tuple[Text, ...]


class HistoryShare(Record):
    stat_id: Text
    parent_stat: Text


class PreparationParameters(Record):
    minutes_stat: Text
    forecast_prior_id: Text
    historical_prior_id: Text
    minimum_player_history: PositiveInt
    donor_minimum_history: PositiveInt
    donor_minutes_lower: Nonnegative
    donor_minutes_upper: Nonnegative
    position_pools: Annotated[tuple[PositionPool, ...], Field(min_length=1)]
    history_shares: tuple[HistoryShare, ...]
    historical_games_lower: Nonnegative
    historical_games_upper: Nonnegative
    evidence: Evidence


class PreparationModel(CalculationModel):
    format_version: Annotated[int, Field(ge=4, le=4)]
    preparation: PreparationParameters


class MarketParameters(Record):
    volatility: Nonnegative
    samples: PositiveInt
    seed: Natural
    normalization_samples: PositiveInt
    normalization_seed: Natural
    wealth_lower: Nonnegative
    wealth_upper: Nonnegative
    wealth_exponent: Nonnegative
    competition_bid: PositiveInt
    evidence: Evidence


class SolverParameters(Record):
    time_limit_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    value_tolerance: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    bound_multipliers: Annotated[tuple[Nonnegative, ...], Field(min_length=1)]
    nomination_count: PositiveInt
    result_decimals: Annotated[int, Field(ge=0, le=12)]
    evidence: Evidence


class CategoryFloor(Record):
    id: Text
    value: Annotated[float, Field(gt=0, allow_inf_nan=False)]


class FitParameters(Record):
    samples: PositiveInt
    seed: Natural
    opponent_seed: Natural
    health_samples: PositiveInt
    health_seed: Natural
    health_blocks: PositiveInt
    mean_missed_games: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    forecast_days: PositiveInt
    unavailable_status: Text
    availability_floor: Annotated[float, Field(gt=0, le=1)]
    category_floors: tuple[CategoryFloor, ...]
    bandwidth: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    gradient_fraction: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    gradient_floor: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    steps: tuple[Annotated[float, Field(gt=0, le=1)], ...]
    improvement_tolerance: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    evidence: Evidence


class AuctionModel(PreparationModel):
    format_version: Annotated[int, Field(ge=5, le=5)]
    market: MarketParameters
    solver: SolverParameters
    fit: FitParameters


class ManagementParameters(Record):
    long_forecast_days: PositiveInt
    candidate_limit: PositiveInt
    reserve_adds: Natural
    minimum_gain: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    opportunity_cost: Nonnegative
    evidence: Evidence


class SeasonModel(AuctionModel):
    format_version: Annotated[int, Field(ge=6, le=6)]
    management: ManagementParameters


class ModelDocument(
    RootModel[
        ModelConfig
        | ResourceModel
        | CalculationModel
        | PreparationModel
        | AuctionModel
        | SeasonModel
    ]
):
    pass


class ConfigRef(Record):
    schema_id: Text
    input_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    effective_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ConfigBundle(Record):
    league: ConfigRef
    season: ConfigRef
    model: ConfigRef


class ValidatedConfig(Record):
    league: LeagueRules
    season: SeasonConfig
    model: (
        ModelConfig
        | ResourceModel
        | CalculationModel
        | PreparationModel
        | AuctionModel
        | SeasonModel
    )
    refs: ConfigBundle
