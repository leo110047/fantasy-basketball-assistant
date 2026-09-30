from pydantic import AwareDatetime, JsonValue

from fba.contracts.base import Record, Text
from fba.contracts.data import Digest
from fba.contracts.inseason import (
    AdjustmentLedger,
    FrozenPriors,
    InseasonLeague,
    InseasonParameters,
    PlayerSnapshot,
    Probability,
)


class ProjectionStudySeason(Record):
    league: InseasonLeague
    players: PlayerSnapshot
    priors: FrozenPriors
    ledger: AdjustmentLedger


class AvailabilityObservation(Record):
    player_id: Text
    game_id: Text
    status: Text
    known_at: AwareDatetime
    tipoff: AwareDatetime
    played: bool
    outcome_known_at: AwareDatetime


class CalibrationObservation(Record):
    predicted: Probability
    observed: Probability
    prior_predicted: Probability


class BacktestStudy(Record):
    training: ProjectionStudySeason
    validation: ProjectionStudySeason
    k_candidates: tuple[float, ...]
    checkpoints: tuple[int, ...]
    minute_k_candidates: tuple[float, ...]
    half_life_candidates: tuple[float, ...]
    shot_k_candidates: tuple[float, ...]
    production_sigma_candidates: tuple[float, ...]
    training_availability: tuple[AvailabilityObservation, ...]
    validation_availability: tuple[AvailabilityObservation, ...]
    training_outcomes: tuple[CalibrationObservation, ...]
    validation_outcomes: tuple[CalibrationObservation, ...]
    dataset_evidence: Text


class BacktestReport(Record):
    training_season: Text
    validation_season: Text
    input_sha256: Digest
    parameters_sha256: Digest
    fitted_k: dict[str, float]
    fitted_calibration: float
    availability_rows: tuple[dict[str, JsonValue], ...]
    production_rows: tuple[dict[str, JsonValue], ...]
    availability_passed: bool
    production_passed: bool
    fitted_parameters: InseasonParameters
    flag_rows: tuple[dict[str, JsonValue], ...]
    projection_rows: tuple[dict[str, JsonValue], ...]
    calibration_rows: tuple[dict[str, JsonValue], ...]
    blended_brier: float
    prior_brier: float
    projection_passed: bool
    calibration_passed: bool
    dataset_evidence: Text
    unverified: tuple[Text, ...]

    @property
    def passed(self) -> bool:
        return (
            self.projection_passed
            and self.calibration_passed
            and self.availability_passed
            and self.production_passed
        )
