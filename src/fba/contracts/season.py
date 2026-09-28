from dataclasses import dataclass
from datetime import date
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from fba.contracts.base import Finite, Nonnegative, Record, Text


class ManagedPlayer(Record):
    id: Text
    expected_games: Nonnegative
    healthy_games: Nonnegative
    season_games: Nonnegative
    return_on: date | None
    game_days: tuple[date, ...]
    means: tuple[Nonnegative, ...]
    covariance: tuple[tuple[Finite, ...], ...]


class ManagementInput(Record):
    stat_ids: tuple[Text, ...]
    sampling_ids: tuple[Text, ...]
    players: tuple[ManagedPlayer, ...]


class MarginalTask(Record):
    index: int
    player: int
    rest: tuple[int, ...]


class MarginalFeature(Record):
    index: int
    values: tuple[Finite, ...]


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


class SeasonKernel(Protocol):
    def __call__(
        self, arrays: SeasonArrays, rosters: tuple[tuple[int, ...], ...], pool: tuple[int, ...]
    ) -> NDArray[np.float64]: ...
