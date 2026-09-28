from typing import Literal

from fba.contracts.base import FormatVersion, PositiveInt, Record
from fba.contracts.config import ConfigBundle, ValidatedConfig
from fba.contracts.data import Digest, Identity
from fba.contracts.projection import CalculationResult, EvaluationResult


class ForecastArchive(Record):
    format_version: FormatVersion
    config: ValidatedConfig
    season_games: PositiveInt
    calculation: CalculationResult
    identities: tuple[Identity, ...]


class AnnualEvaluation(Record):
    format_version: FormatVersion
    snapshot_sha256: Digest
    config: ConfigBundle
    forecast_sha256: Digest
    actual_sha256: Digest
    information_mode: Literal["published", "retrospective"]
    evaluation: EvaluationResult
