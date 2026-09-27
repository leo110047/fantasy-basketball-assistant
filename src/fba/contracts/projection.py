from datetime import date
from typing import Annotated

from pydantic import Field

from fba.contracts.base import (
    Finite,
    FormatVersion,
    Natural,
    Nonnegative,
    PositiveInt,
    Record,
    Text,
)
from fba.contracts.config import ConfigBundle, ValidatedConfig
from fba.contracts.data import Artifact, Digest


class Prior(Record):
    id: Text
    expected_games: Nonnegative
    minutes: Nonnegative
    stats: tuple[Nonnegative, ...]


class ProjectionPlayer(Record):
    id: Text
    name: Text
    team_id: Text | None
    priors: tuple[Prior, ...]
    games_cap: Nonnegative | None
    return_on: date | None
    history: tuple[tuple[Nonnegative, ...], ...]


class ResourcePlayer(ProjectionPlayer):
    catalog: bool
    minutes_sd: Nonnegative


class ProjectionTeam(Record):
    id: Text
    dates: tuple[date, ...]
    full_season_games: PositiveInt


class ResourceTeam(ProjectionTeam):
    possession_budget: Nonnegative


class FrozenCalculationInput(Record):
    format_version: FormatVersion
    config: ValidatedConfig
    artifacts: tuple[Artifact, ...]


class ResourceInput(FrozenCalculationInput):
    players: tuple[ResourcePlayer, ...]
    teams: tuple[ResourceTeam, ...]


class ProjectionInput(FrozenCalculationInput):
    format_version: Annotated[int, Field(ge=2, le=2)]
    players: tuple[ProjectionPlayer, ...]
    teams: tuple[ProjectionTeam, ...]


class Projected(Record):
    id: Text
    expected_games: Nonnegative
    minutes: Nonnegative
    stats: tuple[Nonnegative, ...]
    covariance: tuple[tuple[Finite, ...], ...]


class CategoryScore(Record):
    id: Text
    z: Finite


class CategoryNormalization(Record):
    id: Text
    ratio: Finite | None
    mean: Finite
    deviation: Nonnegative


class ValuationRuler(Record):
    replacement_score: Finite
    categories: tuple[CategoryNormalization, ...]


class PlayerValue(Record):
    id: Text
    fair: Nonnegative | None
    utility: Finite | None
    categories: tuple[CategoryScore, ...]
    unavailable_reason: Text | None


class Valuation(Record):
    replacement_score: Finite
    players: tuple[PlayerValue, ...]


class CalculationResult(Record):
    format_version: FormatVersion
    algorithm: Text
    input_sha256: Digest
    config: ConfigBundle
    projections: tuple[Projected, ...]
    valuation: Valuation


class PredictionVariant(Record):
    id: Text
    players: tuple[Projected, ...]


class EvaluationInput(FrozenCalculationInput):
    stat_ids: tuple[Text, ...]
    season_games: PositiveInt
    actual: tuple[Projected, ...]
    predictions: tuple[PredictionVariant, ...]
    common_ids: tuple[Text, ...]


class EvaluationMetrics(Record):
    id: Text
    predicted_players: PositiveInt
    common_players: PositiveInt
    top_draft_hits: Natural
    rank_correlation: Finite
    median_dollar_error: Nonnegative
    games_mae: Nonnegative


class EvaluationResult(Record):
    format_version: FormatVersion
    algorithm: Text
    input_sha256: Digest
    config: ConfigBundle
    ruler: ValuationRuler
    variants: tuple[EvaluationMetrics, ...]
