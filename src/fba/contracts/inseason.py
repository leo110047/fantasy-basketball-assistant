"""Provider-neutral contracts for the independent in-season application."""

from datetime import date, time
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from fba.contracts.base import Finite, Natural, Nonnegative, PositiveInt, Record, Text
from fba.contracts.config import (
    Category,
    DistributionParameters,
    Evidence,
    InjurySlot,
    Matchup,
    StarterSlot,
    Term,
)
from fba.contracts.data import Digest
from fba.contracts.formula import ArrayFormulaTrace, FormulaTrace

Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class NumberParameter(Record):
    value: Finite
    evidence: Evidence


class IntegerParameter(Record):
    value: Natural
    evidence: Evidence


class PriorPredictiveParameter(Record):
    value: Literal["nested_poisson"]
    evidence: Evidence


class Shot(Record):
    id: Text
    made: Text
    attempted: Text


class DerivedStat(Record):
    id: Text
    kind: Literal["linear", "threshold"]
    terms: tuple[Term, ...]
    threshold: Nonnegative
    minimum_hits: PositiveInt


class AdjustmentField(Record):
    id: Text
    label: Text
    kind: Literal["override", "multiply", "status"]
    targets: Annotated[tuple[Text, ...], Field(min_length=1)]
    minimum: Finite
    maximum: Finite
    statuses: dict[str, Probability]
    only_back_to_back: bool


class PriorGroup(Record):
    id: Text
    positions: tuple[Text, ...]
    minimum_minutes: Nonnegative
    maximum_minutes: Nonnegative


class InseasonParameters(Record):
    format_version: Literal[1]
    version: Text
    rate_k: dict[str, NumberParameter]
    shot_k: dict[str, NumberParameter]
    minute_k: NumberParameter
    minute_half_life: NumberParameter
    availability: dict[str, NumberParameter]
    role_window: IntegerParameter
    role_threshold: NumberParameter
    override_window: IntegerParameter
    override_threshold: NumberParameter
    production_sigma: NumberParameter
    simulations: IntegerParameter
    season_simulations: IntegerParameter
    seed: IntegerParameter
    prior_predictive: PriorPredictiveParameter
    calibration: NumberParameter
    week_calibration: NumberParameter
    safe_probability: NumberParameter
    abandon_probability: NumberParameter
    shortlist: IntegerParameter
    drop_shortlist: IntegerParameter
    weekly_exact_candidates: IntegerParameter
    lineup_batch: IntegerParameter
    scenario_cache_entries: IntegerParameter
    beam_width: IntegerParameter
    max_trade_players: IntegerParameter
    beta_rank: NumberParameter
    beta_need: NumberParameter
    acceptance_threshold: NumberParameter
    acceptance_noise: NumberParameter
    fit_minimum: IntegerParameter
    rank_exponent: NumberParameter
    rank_scale: NumberParameter
    calibration_bins: IntegerParameter
    calibration_alert: NumberParameter
    calibration_minimum: IntegerParameter
    calibration_confidence_z: NumberParameter
    tolerance: NumberParameter
    retry_count: IntegerParameter
    retry_seconds: NumberParameter
    request_timeout: NumberParameter
    maximum_requests: IntegerParameter
    budgets: dict[str, NumberParameter]
    prior_groups: tuple[PriorGroup, ...]
    group_evidence: Evidence
    fields: tuple[AdjustmentField, ...]
    fields_evidence: Evidence


class ProjectionRules(Record):
    categories: Annotated[tuple[Category, ...], Field(min_length=1)]
    season_id: Text
    timezone: Text
    starts_on: date
    ends_on: date
    base_stats: Annotated[tuple[Text, ...], Field(min_length=1)]
    shots: tuple[Shot, ...]
    derived: tuple[DerivedStat, ...]
    players_on_court: PositiveInt
    regulation_minutes: PositiveInt


class InseasonLeague(ProjectionRules):
    format_version: Literal[1]
    league_id: Text
    game_key: Text
    name: Text
    teams: Annotated[int, Field(ge=2)]
    positions: tuple[Text, ...]
    starter_slots: Annotated[tuple[StarterSlot, ...], Field(min_length=1)]
    bench_slots: Natural
    injury_slots: tuple[InjurySlot, ...]
    scoring: Literal["h2h_one_win", "h2h_each_category"]
    category_ties: Literal["exclude", "use_tie_value"]
    week_tie_value: Probability
    adds_per_week: Natural
    effective: Literal["same_day", "next_day"]
    cutoff_local_time: time
    waiver_days: Natural
    lineup_lock: Literal["daily", "player_game"]
    lineup_lock_time: time
    matchups: Annotated[tuple[Matchup, ...], Field(min_length=1)]
    playoff_teams: PositiveInt
    playoff_seeding: Literal["overall", "unknown"] = "unknown"
    playoff_weeks: tuple[Text, ...]
    trade_deadline: AwareDatetime | None
    yahoo_settings_sha256: Digest


class InseasonPreferences(Record):
    format_version: Literal[1]
    preferred_port: Annotated[int, Field(ge=0, le=65535)]
    port_attempts: PositiveInt
    shutdown_seconds: Nonnegative
    sync_interval_seconds: PositiveInt
    stale_warning_seconds: PositiveInt
    stale_limit_seconds: PositiveInt
    timezone: Text
    reserve_adds: Natural
    future_weight: Nonnegative
    trade_value_min_ratio: Annotated[float, Field(ge=0.5, le=1.0)] = 0.7
    untouchable: tuple[Text, ...]
    ignored_opponents: tuple[Text, ...]
    selected_league: Text | None


class SeasonPlayer(Record):
    provider_ids: dict[str, Text] = {}
    team_abbreviation: Text | None = None
    id: Text
    name: Text
    team_id: Text
    positions: Annotated[tuple[Text, ...], Field(min_length=1)]
    status: Text
    known_at: AwareDatetime
    public_rank: PositiveInt | None
    ownership: Probability | None
    ownership_change: Finite | None
    return_on: date | None = None  # Unknown when the source does not publish an estimate.


class SeasonGame(Record):
    id: Text
    home: Text
    away: Text
    tipoff: AwareDatetime
    known_at: AwareDatetime
    status: Literal["scheduled", "in_progress", "completed", "postponed", "cancelled"]


class BoxScore(Record):
    player_id: Text
    game_id: Text
    team_id: Text
    played_at: AwareDatetime
    known_at: AwareDatetime
    minutes: Nonnegative
    stats: dict[str, Nonnegative]


class PlayerPrior(Record):
    # None identifies old imports that discarded playing opportunities.
    appearance_probability: Probability | None = None
    player_id: Text
    minutes: Nonnegative
    rates: dict[str, Nonnegative]
    probabilities: dict[str, Probability]


class FrozenPriors(Record):
    format_version: Literal[1]
    season_id: Text
    version: Text
    source_sha256: Digest
    known_at: AwareDatetime
    players: tuple[PlayerPrior, ...]
    distribution: DistributionParameters | None = None


class PlayerSnapshot(Record):
    format_version: Literal[1]
    season_id: Text
    source: Text
    as_of: AwareDatetime
    players: tuple[SeasonPlayer, ...]
    games: tuple[SeasonGame, ...]
    boxes: tuple[BoxScore, ...]


class FantasyTeam(Record):
    id: Text
    name: Text
    players: tuple[Text, ...]
    injury_players: dict[str, Text]
    selected_slots: dict[str, Text]
    adds_used: Natural | None
    wins: Nonnegative
    losses: Nonnegative
    ties: Nonnegative
    seed: PositiveInt


class SeasonPairing(Record):
    week_id: Text
    home: Text
    away: Text
    elimination: bool | None = None


class ActualScore(Record):
    week_id: Text
    team_id: Text
    through: AwareDatetime
    complete_through: AwareDatetime | None = None
    totals: dict[str, Nonnegative]
    final: bool


class FreeAgent(Record):
    player_id: Text
    status: Literal["free", "waiver"]
    clears_at: AwareDatetime | None


class PlayerOwnership(Record):
    value: Probability | None
    change: Finite | None
    as_of: AwareDatetime
    coverage_type: Text | None


class LeagueSnapshot(Record):
    ownership: dict[str, PlayerOwnership] = {}
    format_version: Literal[1]
    league_id: Text
    as_of: AwareDatetime
    settings_sha256: Digest
    mine: Text
    teams: tuple[FantasyTeam, ...]
    pairings: tuple[SeasonPairing, ...]
    actual: tuple[ActualScore, ...]
    free_agents: tuple[FreeAgent, ...]
    unresolved_rostered: tuple[Text, ...]
    unresolved_free: tuple[Text, ...]


class AdjustmentEntry(Record):
    id: Text
    group_id: Text
    player_id: Text
    team_id: Text
    field: Text
    value: Finite | Text
    starts_on: date
    ends_on: date
    reason: Text
    created_at: AwareDatetime
    replaces: Text | None
    revokes: tuple[Text, ...]


class AdjustmentLedger(Record):
    format_version: Literal[1]
    entries: tuple[AdjustmentEntry, ...]


class ProjectionFlag(Record):
    traces: tuple[FormulaTrace, ...] = ()
    suggestions: dict[Text, FormulaTrace] = {}
    id: Text
    player_id: Text
    field: Text
    kind: Literal["role", "production", "override", "team_changed", "missing_role"]
    model: Finite
    observed: Finite | None
    reason: Text


class EffectivePlayer(Record):
    player: SeasonPlayer
    minutes: Nonnegative
    probability: Probability
    rates: dict[str, Nonnegative]
    probabilities: dict[str, Probability]
    expected: dict[str, Nonnegative]
    prior: PlayerPrior
    observed_minutes: tuple[Nonnegative, ...]
    observed_dates: tuple[date, ...]
    current_totals: dict[str, Nonnegative]
    current_minutes: Nonnegative
    weights: dict[str, Probability]
    traces: dict[str, FormulaTrace]
    adjustments: tuple[AdjustmentEntry, ...]
    flags: tuple[ProjectionFlag, ...]


class EffectiveProjection(Record):
    as_of: AwareDatetime
    on: date
    parameter_version: Text
    players: tuple[EffectivePlayer, ...]


class CategoryForecast(Record):
    value_axes: tuple[Text, ...] = ()
    value_traces: tuple[ArrayFormulaTrace, ...] = ()  # Do not invent trace for older records.
    id: Text
    label: Text
    home: Finite
    away: Finite
    home_numerator: Finite
    home_denominator: Finite | None
    away_numerator: Finite
    away_denominator: Finite | None
    raw_probability: Probability
    probability: Probability
    standard_error: Nonnegative
    z: Finite
    normal_probability: Probability
    strategy: Literal["safe", "key", "abandon"]
    traces: tuple[FormulaTrace, ...]


class DayLineup(Record):
    on: date
    team_id: Text
    slots: dict[str, Text]
    bench: tuple[Text, ...]


class InjuryReturn(Record):
    player_id: Text
    effective_on: date
    drop: Text | None
    estimated: bool


class MatchupPriority(Record):
    status: Literal["normal", "must_win", "eliminated", "unknown"]
    reason: Text
    qualification_if_loss: bool | None = None
    qualification_if_win: bool | None = None


class WeekForecast(Record):
    priority: MatchupPriority | None = None
    elapsed_days: Natural = 0
    remaining_games: dict[str, Natural] = {}
    adds_remaining: Natural | None = None
    no_moves_score: Nonnegative | None = None
    no_moves_raw_score: Nonnegative | None = None
    injury_returns: tuple[InjuryReturn, ...] = ()
    lineup_search: Literal["joint_exact", "daily_exact_coordinate"] = "daily_exact_coordinate"
    sample_coupling: dict[Text, Literal["game_id", "exchangeable_count_v1"]] = {}
    prior_players: tuple[Text, ...] = ()
    week_id: Text
    home: Text
    away: Text
    scoring: Literal["h2h_one_win", "h2h_each_category"]
    raw_score: Nonnegative
    score: Nonnegative
    standard_error: Nonnegative
    categories: tuple[CategoryForecast, ...]
    lineups: tuple[DayLineup, ...]
    simulations: PositiveInt
    opponent_policy: Literal["fixed_roster"]
    traces: tuple[FormulaTrace, ...]


class SyncState(Record):
    forecast_error: Text | None = None
    connected: bool
    authorization_valid: bool
    last_success: AwareDatetime | None
    last_error: Text | None
    settings_pending: bool
    unresolved_rostered: tuple[Text, ...]


class FeatureAvailability(Record):
    enabled: bool
    reason: Text | None
    repair: Text | None
    as_of: AwareDatetime | None


class Proposal(Record):
    id: Text
    created_at: AwareDatetime
    opponent: Text
    send: tuple[Text, ...]
    receive: tuple[Text, ...]
    rank_delta: Finite
    need_delta: Finite
    probability: Probability
    outcome: Literal["pending", "accepted", "rejected", "withdrawn"]
    supersedes: Text | None


class CalculationTimeout(TimeoutError):
    """A configured calculation or synchronization deadline expired."""


class AuthorizationRequired(ValueError):
    """Credentials are absent or revoked; no league calculation is permitted."""
