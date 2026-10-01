from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue

from fba.contracts.base import Finite, Natural, Nonnegative, Record, Text
from fba.contracts.data import Digest
from fba.contracts.formula import FormulaTrace
from fba.contracts.inseason import DayLineup, MatchupPriority, Probability, WeekForecast


class RosterMove(Record):
    add: Text
    drop: Text
    effective_on: date
    starter_games: Natural


class TradePartner(Record):
    team_id: Text
    score: Finite
    categories: tuple[Text, ...]
    traces: tuple[FormulaTrace, ...]


class AddPlan(Record):
    priority: MatchupPriority | None = None
    category_changes: dict[Text, dict[Text, FormulaTrace]] = {}
    id: Text
    moves: tuple[RosterMove, ...]
    before: WeekForecast
    after: WeekForecast
    delta_week: Finite
    delta_season: Finite | None
    delta_strength: Finite | None = None
    strength_unavailable: Text | None = None
    score: Finite
    traces: tuple[FormulaTrace, ...]


class TradeSummary(Record):
    acceptance_unavailable: Text | None = None
    opponent: Text
    send: tuple[Text, ...]
    receive: tuple[Text, ...]
    rosters: dict[str, tuple[Text, ...]]
    automatic_adds: dict[str, tuple[Text, ...]]
    automatic_drops: dict[str, tuple[Text, ...]]
    mine_delta: Finite
    opponent_delta: Finite
    playoff_delta: Finite
    playoff_games_delta: Finite
    rank_delta: Finite | None
    acceptance: Probability | None
    expected_gain: Finite | None
    calibrated: bool


class TradeResult(TradeSummary):
    category_changes: dict[Text, dict[Text, FormulaTrace]] = {}
    opponent_category_changes: dict[Text, dict[Text, FormulaTrace]] = {}
    before: tuple[WeekForecast, ...]
    after: tuple[WeekForecast, ...]
    opponent_before: tuple[WeekForecast, ...]
    opponent_after: tuple[WeekForecast, ...]
    traces: tuple[FormulaTrace, ...]


class TradeSearchResult(Record):
    trades: tuple[TradeSummary, ...]
    counts: dict[Text, Natural]
    minimum_value_ratio: Annotated[float, Field(ge=0.5, le=1.0)]


class TodayAction(Record):
    id: Text
    kind: Literal["start", "bench", "injury_in", "injury_out", "add_drop", "locked", "drop"]
    player_id: Text
    slot: Text | None
    reason: Text
    completed: bool


class TodayPlayer(Record):
    category_traces: dict[Text, tuple[FormulaTrace, ...]] = {}
    player_id: Text
    opponents: tuple[Text, ...]
    tipoffs: tuple[AwareDatetime, ...]
    status: Text
    slot: Text | None
    reason: Text
    marginal: FormulaTrace | None
    category_changes: dict[Text, Finite]


class TodayResult(Record):
    priority: MatchupPriority | None = None
    week_forecast: WeekForecast | None = None
    on: date
    lineup: DayLineup
    actions: tuple[TodayAction, ...]
    locks: dict[str, AwareDatetime]
    score_before: Nonnegative
    score_after: Nonnegative
    plan_id: Text | None
    recommendation: AddPlan | None = None
    injury_pending: tuple[Text, ...] = ()
    players: tuple[TodayPlayer, ...]
    traces: tuple[FormulaTrace, ...]


class TeamPlayerView(Record):
    player_id: Text
    recent_minutes: dict[str, FormulaTrace]
    multipliers: dict[str, FormulaTrace]
    return_on: date | None
    manual_return_on: date | None
    manual_status: Text | None


class TeamProjectionView(Record):
    team_id: Text
    week_games: int | None
    next_week_games: int | None
    back_to_back: tuple[date, ...]
    minutes: FormulaTrace
    budget: FormulaTrace
    difference: FormulaTrace
    players: tuple[TeamPlayerView, ...]


class PredictionRecord(Record):
    id: Text
    created_at: AwareDatetime
    week_id: Text
    parameter_version: Text
    parameter_sha256: Digest
    input_hashes: tuple[Digest, ...]
    ledger_sha256: Digest
    league_sha256: Digest | None = None
    recommendation_policy_sha256: Digest | None = None
    with_adjustments: WeekForecast
    without_adjustments: WeekForecast
    recommendations: tuple[AddPlan, ...]
    proposal_probabilities: dict[str, Probability]
    week_score_kind: Literal["standings_points", "win_probability"] = "standings_points"


class CalibrationBin(Record):
    lower: Probability
    upper: Probability
    count: Natural
    predicted: Probability | None
    observed: Probability | None
    difference: Nonnegative | None
    independent_samples: Natural = 0
    effective_samples: Nonnegative = 0.0
    uncertainty: Nonnegative | None = None
    traces: tuple[FormulaTrace, ...] = ()
    alert: bool = False


class WeeklyReview(Record):
    week_id: Text
    prediction_ids: tuple[Text, ...]
    rows: tuple[dict[str, JsonValue], ...]
    category_brier: Nonnegative
    week_brier: Nonnegative | None
    with_adjustments_mae: Nonnegative
    without_adjustments_mae: Nonnegative
    bins: tuple[CalibrationBin, ...]
    refit_alert: bool
    recommendation_outcomes: tuple[dict[str, JsonValue], ...]
    traces: tuple[FormulaTrace, ...]
    week_score_kind: Literal["standings_points", "win_probability"] = "standings_points"
    week_prediction_ids: tuple[Text, ...] = ()
    week_observations: tuple[dict[str, JsonValue], ...] = ()


class CalibrationObservationRecord(Record):
    prediction_id: Text
    category: Text
    created_at: AwareDatetime
    raw_probability: Probability
    observed: Probability


class CalibrationHistory(Record):
    season_id: Text
    observations: tuple[CalibrationObservationRecord, ...]
    week_observations: tuple[CalibrationObservationRecord, ...] = ()
    snapshot_hashes: tuple[Digest, ...]
