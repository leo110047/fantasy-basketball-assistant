from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from fba.contracts.auction import AuctionResult, DraftState, Sale, TeamBudget
from fba.contracts.base import (
    Finite,
    FormatVersion,
    Natural,
    Nonnegative,
    PositiveInt,
    Record,
    Text,
)
from fba.contracts.config import ConfigBundle, Evidence
from fba.contracts.data import Digest

Regime = Literal["market", "premium", "category"]
Order = Literal["market", "targets", "mixed"]


class StressSettings(Record):
    format_version: FormatVersion
    regimes: Annotated[tuple[Regime, ...], Field(min_length=1)]
    orders: Annotated[tuple[Order, ...], Field(min_length=1)]
    seeds: Annotated[tuple[Natural, ...], Field(min_length=1)]
    premium_per_team: PositiveInt
    premium_multiplier: Annotated[float, Field(ge=1, allow_inf_nan=False)]
    maximum_sweeps: PositiveInt
    evidence: Evidence

    @model_validator(mode="after")
    def unique_dimensions(self) -> Self:
        for name in ("regimes", "orders", "seeds"):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"auction_stress.{name}: duplicate scenario")
        return self


class PathBid(Record):
    player_id: Text
    sweep: Natural
    amount: Natural
    reason: Text | None


class AuctionPath(Record):
    complete: bool
    room: tuple[TeamBudget, ...]
    score: Finite
    first_sale: Sale | None
    sales: tuple[Sale, ...]
    bids: tuple[PathBid, ...]
    solver_calls: Natural
    policy_calls: Natural


class PathPair(Record):
    regime: Regime
    order: Order
    seed: Natural
    participate: AuctionPath
    skip: AuctionPath
    delta: Finite | None


class StressResult(Record):
    format_version: FormatVersion
    config: ConfigBundle
    input_sha256: Digest
    state_sha256: Digest
    settings_sha256: Digest
    settings: StressSettings
    state: DraftState
    initial: AuctionResult
    mode: Literal["equal", "fit"]
    player_id: Text
    ceiling: PositiveInt
    verdict: Literal["incomplete", "no_difference", "supported", "skip", "mixed"]
    complete_pairs: Natural
    pairs: tuple[PathPair, ...]
    policy_calls: Natural
    solver_calls: Natural
    calculation_seconds: Nonnegative
    paths_seconds: Nonnegative
