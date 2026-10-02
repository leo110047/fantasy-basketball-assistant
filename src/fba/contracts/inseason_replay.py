"""Dated, offline validation inputs. These never replace live Yahoo rosters."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from fba.contracts.base import Finite, PositiveInt, Record, Text
from fba.contracts.config import Evidence
from fba.contracts.data import Digest
from fba.contracts.inseason import (
    AdjustmentLedger,
    FrozenPriors,
    InseasonLeague,
    InseasonPreferences,
    LeagueSnapshot,
    PlayerSnapshot,
    Probability,
    WeekForecast,
)
from fba.contracts.inseason_results import RosterMove


class ReplayCase(Record):
    league: InseasonLeague
    players: PlayerSnapshot
    priors: FrozenPriors
    ledger: AdjustmentLedger
    snapshot: LeagueSnapshot
    as_of: AwareDatetime
    week_id: Text
    final_players: PlayerSnapshot
    final_snapshot: LeagueSnapshot


class PolicyReplayStudy(Record):
    cases: Annotated[tuple[ReplayCase, ...], Field(min_length=1)]
    preferences: InseasonPreferences
    recall_target: Annotated[float, Field(ge=0.95, le=1)]
    minimum_cases: PositiveInt
    oracle_max_plans: PositiveInt
    evidence: Evidence


class PolicyOutcome(Record):
    policy: Literal["unchanged", "ranking", "recommended"]
    forecast: WeekForecast
    moves: tuple[RosterMove, ...]
    actual_score: Finite
    actual_categories: dict[str, Finite]


class ReplayRow(Record):
    league_id: Text
    season_id: Text
    week_id: Text
    as_of: AwareDatetime
    input_sha256: Digest
    outcomes: tuple[PolicyOutcome, ...]
    full_candidates: int
    shortlist_retained_best: bool | None
    full_plan_count: int
    search_retained_best: bool | None


class PolicyReplayReport(Record):
    search_method: Literal["z_beam", "complete"] = "z_beam"
    input_sha256: Digest
    parameters_sha256: Digest
    rows: tuple[ReplayRow, ...]
    mean_scores: dict[str, Finite]
    recall: Probability | None
    beam_recall: Probability | None
    recall_passed: bool
    policy_passed: bool
    sufficient_cases: bool
    evidence: Evidence
    scope: Text
