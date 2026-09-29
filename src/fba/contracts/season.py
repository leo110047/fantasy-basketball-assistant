from dataclasses import dataclass, replace
from datetime import date
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from fba.contracts.base import Finite, Natural, Nonnegative, Record, Text


class ManagedPlayer(Record):
    id: Text
    expected_games: Nonnegative
    healthy_games: Nonnegative
    season_games: Nonnegative
    return_on: date | None
    game_days: tuple[date, ...]
    means: tuple[Nonnegative, ...]
    covariance: tuple[tuple[Finite, ...], ...]


class RoleManagedPlayer(ManagedPlayer):
    unconstrained_games: Nonnegative


class ManagementInput(Record):
    stat_ids: tuple[Text, ...]
    sampling_ids: tuple[Text, ...]
    players: tuple[RoleManagedPlayer | ManagedPlayer, ...]


@dataclass(frozen=True)
class ReplaySchedule:
    days: tuple[date, ...]
    known: tuple[NDArray[np.bool_], ...]
    actual: NDArray[np.bool_]


class MarginalTask(Record):
    index: int
    player: int
    rest: tuple[int, ...]
    opponents: tuple[tuple[int, ...], ...]


class MarginalFeature(Record):
    index: int
    values: tuple[Finite, ...]


class ManagementPolicy(Record):
    streaming_slots: tuple[Natural, ...]
    reserve_adds: Natural
    upgrades: bool


class SeasonEvent(Record):
    day: Natural
    team: Natural
    kind: Literal[
        "return_release",
        "return_drop",
        "activate",
        "il",
        "injury_add",
        "upgrade",
        "stream",
        "lineup",
    ]
    dropped: int | None
    added: int | None
    active: tuple[int, ...]
    injured: tuple[int, ...]
    started: tuple[int, ...]


@dataclass(frozen=True)
class TacticalArrays:
    policy: ManagementPolicy
    short_values: NDArray[np.float64]
    long_values: NDArray[np.float64]
    acquired_short: NDArray[np.float64]
    acquired_long: NDArray[np.float64]
    short_orders: NDArray[np.int32]
    long_orders: NDArray[np.int32]
    candidate_limit: int
    minimum_gain: float
    opportunity_cost: float

    def with_policy(self, policy: ManagementPolicy) -> "TacticalArrays":
        return replace(self, policy=policy)


@dataclass(frozen=True)
class SeasonRun:
    counts: NDArray[np.float64]
    adds: NDArray[np.int32]
    events: tuple[SeasonEvent, ...]


@dataclass(frozen=True)
class SeasonArrays:
    """Validated invocation-local numeric ABI; arrays never escape as public results."""

    health: NDArray[np.uint8]
    games: NDArray[np.uint8]
    weeks: NDArray[np.int32]
    periods: NDArray[np.int32]
    masks: NDArray[np.uint64]
    slots: NDArray[np.uint64]
    priority: NDArray[np.float64]
    value: NDArray[np.float64]
    orders: NDArray[np.int32]
    il_eligible: NDArray[np.uint8]
    lock_days: NDArray[np.uint8]
    waiver_days: int
    add_limit: int
    next_day: bool
    weekly_lock: bool
    roster_capacity: int
    week_count: int
    known_week_games: NDArray[np.int32] | None = None


class SeasonKernel(Protocol):
    def __call__(
        self, arrays: SeasonArrays, rosters: tuple[tuple[int, ...], ...], pool: tuple[int, ...]
    ) -> NDArray[np.float64]: ...

    def run(
        self,
        arrays: SeasonArrays,
        rosters: tuple[tuple[int, ...], ...],
        pool: tuple[int, ...],
        tactics: TacticalArrays | None,
        trace: bool,
        *,
        primary_only: bool = False,
    ) -> SeasonRun: ...
