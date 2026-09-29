"""Runtime contracts are re-exported; only weekly advice below remains design-only.

The future advisor shares core.projection, core.valuation and core.managed/season
with the auction and replay. Its adapter must freeze and verify all source bytes,
filter observations by published_at <= as_of, and reject missing required inputs.
No provider lookup or future replay outcomes may enter the decision function.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from fba.contracts.auction import Assignment as Assignment
from fba.contracts.auction import AuctionInput as AuctionInput
from fba.contracts.auction import AuctionResult as AuctionResult
from fba.contracts.auction import Cap as Cap
from fba.contracts.auction import DraftOverride as DraftOverride
from fba.contracts.auction import DraftState as DraftState
from fba.contracts.auction import DraftTeam as DraftTeam
from fba.contracts.auction import Infeasible as Infeasible
from fba.contracts.auction import MarketPrice as MarketPrice
from fba.contracts.auction import Plan as Plan
from fba.contracts.auction import Sale as Sale
from fba.contracts.auction import SolverError as SolverError
from fba.contracts.backtest import HealthObservation as HealthObservation
from fba.contracts.backtest import ScheduleObservation as ScheduleObservation
from fba.contracts.base import ConfigError as ConfigError
from fba.contracts.base import DataError as DataError
from fba.contracts.base import IdentityError as IdentityError
from fba.contracts.base import VersionError as VersionError
from fba.contracts.config import ConfigBundle as ConfigBundle
from fba.contracts.config import ConfigRef as ConfigRef
from fba.contracts.config import LeagueRules as LeagueRules
from fba.contracts.config import ModelDocument as ModelDocument
from fba.contracts.config import SeasonConfig as SeasonConfig
from fba.contracts.config import ValidatedConfig as ValidatedConfig
from fba.contracts.data import Artifact as Artifact
from fba.contracts.data import Calibration as Calibration
from fba.contracts.data import Digest as Digest
from fba.contracts.data import Forecast as Forecast
from fba.contracts.data import Game as Game
from fba.contracts.data import Identity as Identity
from fba.contracts.data import Player as Player
from fba.contracts.data import PlayerGame as PlayerGame
from fba.contracts.data import Provenance as Provenance
from fba.contracts.data import Snapshot as Snapshot
from fba.contracts.data import StatValue as StatValue
from fba.contracts.projection import CalculationResult as CalculationResult
from fba.contracts.projection import CategoryScore as CategoryScore
from fba.contracts.projection import PlayerValue as PlayerValue
from fba.contracts.projection import Projected as Projected
from fba.contracts.season import ManagementInput as ManagementInput
from fba.contracts.season import SeasonEvent as SeasonEvent

@dataclass(frozen=True)
class InjuryPlacement:
    player_id: str
    group_id: str

@dataclass(frozen=True)
class AddsUsed:
    period_id: str
    injury: int
    upgrade: int
    stream: int

@dataclass(frozen=True)
class Roster:
    team_id: str
    active: tuple[str, ...]
    injured: tuple[InjuryPlacement, ...]
    locked_lineup: tuple[Assignment, ...]
    lock_until: datetime
    adds_used: AddsUsed

@dataclass(frozen=True)
class KnownAtDay:
    as_of: datetime
    # As-of eligibility and team identities; frozen source artifacts are required.
    players: tuple[Player, ...]
    schedule: tuple[Game, ...]
    schedule_updates: tuple[ScheduleObservation, ...]
    health: tuple[HealthObservation, ...]
    projections: tuple[Projected, ...]
    management: ManagementInput
    artifacts: tuple[Artifact, ...]

@dataclass(frozen=True)
class AddDrop:
    team_id: str
    add: str
    drop: str | None
    effective_at: datetime
    purpose: Literal["injury", "upgrade", "stream"]

@dataclass(frozen=True)
class ILMove:
    team_id: str
    player_id: str
    target_group: str | None
    effective_at: datetime

@dataclass(frozen=True)
class WaiverRelease:
    player_id: str
    eligible_at: datetime

@dataclass(frozen=True)
class SeasonState:
    day: date
    # Includes both mine and opponent, with IL and adds used in the shared period.
    rosters: tuple[Roster, ...]
    free_agents: tuple[str, ...]
    pending: tuple[AddDrop, ...]
    waivers: tuple[WaiverRelease, ...]

@dataclass(frozen=True)
class DailyDecision:
    adds: tuple[AddDrop, ...]
    il_moves: tuple[ILMove, ...]
    lineups: tuple[tuple[Assignment, ...], ...]

@dataclass(frozen=True)
class WeeklyAdviceInput:
    config: ValidatedConfig
    snapshot_sha256: Digest
    mine: str
    opponent: str
    week_id: str
    state: SeasonState
    known: KnownAtDay

@dataclass(frozen=True)
class ProbabilityChange:
    category_id: str
    before: float
    after: float
    delta: float

@dataclass(frozen=True)
class WeeklyAdvice:
    config: ConfigBundle
    input_sha256: Digest
    as_of: datetime
    adds: tuple[AddDrop, ...]
    il_moves: tuple[ILMove, ...]
    daily_plan: tuple[DailyDecision, ...]
    categories: tuple[ProbabilityChange, ...]
    assumptions: tuple[str, ...]

# Pure, deterministic for identical frozen input and model-configured random seeds.
# Past-week evaluation supplies the same as-of request, then scores unseen outcomes.
def advise_week(request: WeeklyAdviceInput) -> WeeklyAdvice: ...
