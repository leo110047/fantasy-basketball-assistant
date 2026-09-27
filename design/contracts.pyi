"""Stage 0 interface design only; not a runtime module or implementation."""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Generic, Literal, NewType, TypeAlias, TypeVar

PlayerId = NewType('PlayerId', str)
TeamId = NewType('TeamId', str)
StatId = NewType('StatId', str)
CategoryId = NewType('CategoryId', str)
Sha256 = NewType('Sha256', str)
PeriodId = NewType('PeriodId', str)
T = TypeVar('T')

@dataclass(frozen=True)
class ConfigRef:
    schema_id: str
    input_sha256: Sha256
    effective_sha256: Sha256

@dataclass(frozen=True)
class ConfigBundle:
    league: ConfigRef
    season: ConfigRef
    model: ConfigRef

# Runtime config models own the generated schemas in stage 1.
# These names denote immutable instances, never JSON dicts.
class LeagueRules: ...
class SeasonConfig: ...
class ModelConfig: ...

@dataclass(frozen=True)
class ParsedConfig:
    league: LeagueRules
    season: SeasonConfig
    model: ModelConfig
    refs: ConfigBundle

class ValidatedConfig(ParsedConfig): ...

@dataclass(frozen=True)
class Provenance:
    source_id: str
    raw_sha256: Sha256
    published_at: datetime
    effective_at: datetime
    retrieved_at: datetime
    delivery: Literal['fetch', 'manual']
    assumption: str | None

@dataclass(frozen=True)
class KnownQuote:
    amount: float
    provenance: Provenance

@dataclass(frozen=True)
class MissingQuote:
    reason: str

Quote: TypeAlias = KnownQuote | MissingQuote

@dataclass(frozen=True)
class ProviderIdentity:
    provider: str
    provider_player_id: str
    provenance: Provenance

@dataclass(frozen=True)
class Player:
    id: PlayerId
    name: str
    identities: tuple[ProviderIdentity, ...]
    positions: tuple[str, ...]
    position_provenance: Provenance
    nba_team_id: str | None
    team_provenance: Provenance
    projected_quote: Quote
    average_quote: Quote
    history_status: Literal['available', 'no_previous_season_history', 'incomplete']

@dataclass(frozen=True)
class StatValue:
    stat_id: StatId
    value: float

@dataclass(frozen=True)
class Game:
    id: str
    home_team_id: str
    away_team_id: str
    tipoff: datetime
    local_date: date
    status: Literal['scheduled', 'completed', 'postponed', 'cancelled']
    provenance: Provenance

@dataclass(frozen=True)
class PlayerGame:
    player_id: PlayerId
    game_id: str
    team_at_game: str
    stats: tuple[StatValue, ...]
    provenance: Provenance

@dataclass(frozen=True)
class ForecastObservation:
    player_id: PlayerId
    per_game: tuple[StatValue, ...]
    expected_games: float
    provenance: Provenance

@dataclass(frozen=True)
class SnapshotArtifact:
    relative_path: str
    sha256: Sha256
    bytes: int
    media_type: str
    provenance: Provenance

@dataclass(frozen=True)
class Calibration:
    training_season_id: str
    training_cutoff: datetime
    training_inputs: tuple[Sha256, ...]
    method: str
    coefficients: tuple[float, ...]
    sample_size: int

@dataclass(frozen=True)
class Snapshot:
    format_version: int
    version: str
    sha256: Sha256
    as_of: datetime
    config: ConfigBundle
    artifacts: tuple[SnapshotArtifact, ...]
    players: tuple[Player, ...]
    schedule: tuple[Game, ...]
    history: tuple[PlayerGame, ...]
    forecasts: tuple[ForecastObservation, ...]
    calibration: Calibration

@dataclass(frozen=True)
class Sale:
    id: str
    sequence: int
    player_id: PlayerId
    buyer: TeamId
    amount: int

@dataclass(frozen=True)
class Team:
    id: TeamId
    name: str

@dataclass(frozen=True)
class PlayerOverride:
    player_id: PlayerId
    market: KnownQuote | None
    positions: tuple[str, ...] | None
    provenance: Provenance

@dataclass(frozen=True)
class DraftState:
    format_version: int
    draft_id: str
    revision: int
    config: ConfigBundle
    snapshot_sha256: Sha256
    mine: TeamId
    teams: tuple[Team, ...]
    sales: tuple[Sale, ...]
    watch: tuple[PlayerId, ...]
    overrides: tuple[PlayerOverride, ...]

@dataclass(frozen=True)
class Projection:
    player_id: PlayerId
    stat_axis: tuple[StatId, ...]
    means: tuple[float, ...]
    covariance: tuple[tuple[float, ...], ...]
    expected_games: float
    healthy_games: float
    evidence: tuple[Provenance, ...]

@dataclass(frozen=True)
class CategoryValue:
    category_id: CategoryId
    value: float

@dataclass(frozen=True)
class Valuation:
    player_id: PlayerId
    fair: float
    utility: float
    z: tuple[CategoryValue, ...]

@dataclass(frozen=True)
class Draws:
    algorithm: str
    seed: int
    sha256: Sha256
    player_axis: tuple[PlayerId, ...]
    values: tuple[tuple[float, ...], ...]

@dataclass(frozen=True)
class MarketPrice:
    player_id: PlayerId
    expected: Quote
    acquisition: Quote
    planning_cost: int | None  # Only absent with an explicit MissingQuote reason.

@dataclass(frozen=True)
class SlotAssignment:
    slot_id: str
    player_id: PlayerId

@dataclass(frozen=True)
class Plan:
    players: tuple[PlayerId, ...]
    assignments: tuple[SlotAssignment, ...]
    cost: int
    utility: float

@dataclass(frozen=True)
class Infeasible:
    reason: str
    constraint_paths: tuple[str, ...]

@dataclass(frozen=True)
class Cap:
    player_id: PlayerId
    amount: int
    buy_plan: Plan
    skip_plan: Plan | Infeasible

@dataclass(frozen=True)
class UnavailableCap:
    player_id: PlayerId
    reason: str

@dataclass(frozen=True)
class AuctionResult:
    market: tuple[MarketPrice, ...]
    caps: tuple[Cap | UnavailableCap, ...]
    plan: Plan | Infeasible
    nomination_ids: tuple[PlayerId, ...]

@dataclass(frozen=True)
class InjuryPlacement:
    player_id: PlayerId
    group_id: str

@dataclass(frozen=True)
class AddsUsed:
    period_id: PeriodId
    injury: int
    upgrade: int
    stream: int

@dataclass(frozen=True)
class Roster:
    team_id: TeamId
    active: tuple[PlayerId, ...]
    injured: tuple[InjuryPlacement, ...]
    locked_lineup: tuple[SlotAssignment, ...]
    lock_until: datetime
    adds_used: AddsUsed

@dataclass(frozen=True)
class Availability:
    player_id: PlayerId
    designation: str
    playability: Literal['available', 'out', 'unknown']
    expected_return: datetime | None
    provenance: Provenance

@dataclass(frozen=True)
class KnownAtDay:
    as_of: datetime
    # As-of identities, eligible positions and NBA teams; no external lookup.
    players: tuple[Player, ...]
    schedule: tuple[Game, ...]
    availability: tuple[Availability, ...]
    projections: tuple[Projection, ...]

@dataclass(frozen=True)
class AddDrop:
    team_id: TeamId
    add: PlayerId
    drop: PlayerId | None
    effective_at: datetime
    purpose: Literal['injury', 'upgrade', 'stream']

@dataclass(frozen=True)
class ILMove:
    team_id: TeamId
    player_id: PlayerId
    target_group: str | None
    effective_at: datetime

@dataclass(frozen=True)
class WaiverRelease:
    player_id: PlayerId
    eligible_at: datetime

@dataclass(frozen=True)
class SeasonState:
    day: date
    rosters: tuple[Roster, ...]
    free_agents: tuple[PlayerId, ...]
    pending: tuple[AddDrop, ...]
    waivers: tuple[WaiverRelease, ...]

@dataclass(frozen=True)
class DailyDecision:
    adds: tuple[AddDrop, ...]
    il_moves: tuple[ILMove, ...]
    lineups: tuple[tuple[SlotAssignment, ...], ...]

@dataclass(frozen=True)
class WeeklyAdviceInput:
    config: ValidatedConfig
    snapshot_sha256: Sha256
    mine: TeamId
    opponent: TeamId
    week_id: str
    state: SeasonState
    known: KnownAtDay

@dataclass(frozen=True)
class ProbabilityChange:
    category_id: CategoryId
    before: float
    after: float
    delta: float

@dataclass(frozen=True)
class WeeklyAdvice:
    adds: tuple[AddDrop, ...]
    il_moves: tuple[ILMove, ...]
    daily_plan: tuple[DailyDecision, ...]
    categories: tuple[ProbabilityChange, ...]
    assumptions: tuple[str, ...]

@dataclass(frozen=True)
class ComputationIdentity:
    format_version: int
    config: ConfigBundle
    snapshot_sha256: Sha256
    state_sha256: Sha256
    revision: int
    algorithm_version: str
    method: str
    draws_sha256: Sha256

@dataclass(frozen=True)
class Result(Generic[T]):
    identity: ComputationIdentity
    value: T

@dataclass(frozen=True)
class StageTiming:
    stage: str
    elapsed_ns: int
    solver_calls: int

@dataclass(frozen=True)
class ExecutionRecord:
    result_sha256: Sha256
    machine: str
    runtime_versions: tuple[str, ...]
    timings: tuple[StageTiming, ...]

class ConfigError(ValueError):
    field_path: str
class DataError(ValueError):
    artifact: str
    row: int | None
class IdentityError(DataError):
    unresolved: tuple[str, ...]
class SolverError(RuntimeError): ...
class CalculationTimeout(SolverError): ...
class VersionError(ValueError): ...

def validate_config(config: ParsedConfig) -> ValidatedConfig: ...
def project(config: ValidatedConfig, snapshot: Snapshot) -> tuple[Projection, ...]: ...
def value(config: ValidatedConfig, projections: tuple[Projection, ...]) -> tuple[Valuation, ...]: ...
def price_market(config: ValidatedConfig, players: tuple[Player, ...], state: DraftState, values: tuple[Valuation, ...], draws: Draws) -> tuple[MarketPrice, ...]: ...
def solve_portfolio(config: ValidatedConfig, players: tuple[Player, ...], state: DraftState, values: tuple[Valuation, ...], market: tuple[MarketPrice, ...]) -> Plan | Infeasible: ...
def calculate_auction(config: ValidatedConfig, snapshot: Snapshot, state: DraftState, draws: Draws) -> Result[AuctionResult]: ...
def choose_actions(config: ValidatedConfig, state: SeasonState, known: KnownAtDay) -> DailyDecision: ...
def choose_lineup(config: ValidatedConfig, roster: Roster, known: KnownAtDay) -> tuple[SlotAssignment, ...]: ...
def score_week(config: ValidatedConfig, totals: tuple[tuple[StatValue, ...], ...]) -> tuple[tuple[CategoryValue, ...], ...]: ...
def advise_week(request: WeeklyAdviceInput, draws: Draws) -> Result[WeeklyAdvice]: ...
