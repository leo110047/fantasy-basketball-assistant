from datetime import timedelta

import pytest
from inseason_support import fixture, parameters

from fba.contracts.base import DataError
from fba.contracts.inseason_backtest import (
    AvailabilityObservation,
    BacktestStudy,
    CalibrationObservation,
    ProjectionStudySeason,
)
from fba.data.codec import canonical, digest
from fba.inseason.backtest import run_study


def study_season(year):
    league, _, players, priors, ledger, *_ = fixture(year=year)
    return ProjectionStudySeason(league=league, players=players, priors=priors, ledger=ledger)


def availability(year):
    season = study_season(year)
    tipoff = season.players.as_of - timedelta(days=2)
    return tuple(
        AvailabilityObservation(
            player_id="p0",
            game_id=f"{status}:{i}",
            status=status,
            known_at=tipoff - timedelta(hours=1),
            tipoff=tipoff,
            played=bool(i),
            outcome_known_at=tipoff + timedelta(hours=3),
        )
        for status in parameters().availability
        for i in range(2)
    )


def study():
    observations = tuple(
        CalibrationObservation(predicted=p, observed=y, prior_predicted=0.5)
        for p, y in ((0.2, 0.0), (0.8, 1.0), (0.3, 1.0), (0.7, 0.0))
    )
    return BacktestStudy(
        training=study_season(2025),
        validation=study_season(2026),
        k_candidates=(100.0, 300.0),
        checkpoints=(5,),
        minute_k_candidates=(1.0, 3.0),
        half_life_candidates=(5.0, 10.0),
        shot_k_candidates=(100.0, 300.0),
        production_sigma_candidates=(2.0, 3.0),
        training_availability=availability(2025),
        validation_availability=availability(2026),
        training_outcomes=observations,
        validation_outcomes=observations,
        dataset_evidence="Synthetic execution test, not empirical validation",
    )


def test_holdout_report_preserves_failed_gates_and_hashes_fitted_parameters():
    params, data = parameters(), study()
    report = run_study(data, params, digest(canonical(data)), digest(canonical(params)))
    assert report.parameters_sha256 == digest(canonical(report.fitted_parameters))
    assert report.training_season != report.validation_season
    assert {r["stat"] for r in report.projection_rows} >= {"minutes", "FG%", "FT%", "OREB"}
    assert not report.calibration_passed  # Sparse probability bins never get a fake pass.
    assert report.unverified
    assert report.fitted_parameters.rate_k["OREB"].evidence.kind == "backtest"
    assert all(
        p.value == 0.5 and p.evidence.kind == "backtest"
        for p in report.fitted_parameters.availability.values()
    )
    assert len(report.production_rows) == 3
    assert not report.production_passed  # No flagged holdout rows cannot validate the threshold.
    assert {row["kind"] for row in report.flag_rows} == {"role", "production", "override"}


def test_availability_fit_rejects_late_status_duplicate_and_missing_status():
    from fba.inseason.parameter_fit import availability_samples

    season, samples = study_season(2025), availability(2025)
    statuses = set(parameters().availability)
    for rows, match in (
        (samples + (samples[0],), "duplicate"),
        (samples[2:], "missing observations"),
        ((samples[0].model_copy(update={"known_at": samples[0].tipoff}), *samples[1:]), "pregame"),
    ):
        with pytest.raises(DataError, match=match):
            availability_samples(rows, season, statuses)


def test_validation_labels_do_not_change_fitted_availability_or_threshold():
    data, params = study(), parameters()
    modified = data.model_copy(
        update={
            "validation_availability": tuple(
                row.model_copy(update={"played": True}) for row in data.validation_availability
            )
        }
    )
    original = run_study(data, params, digest(canonical(data)), digest(canonical(params)))
    changed = run_study(modified, params, digest(canonical(modified)), digest(canonical(params)))
    assert {k: p.value for k, p in changed.fitted_parameters.availability.items()} == {
        k: p.value for k, p in original.fitted_parameters.availability.items()
    }
    assert (
        changed.fitted_parameters.production_sigma.value
        == original.fitted_parameters.production_sigma.value
    )
    assert changed.availability_rows != original.availability_rows


def test_flag_validation_counts_observed_false_positives_instead_of_only_hits():
    from fba.inseason.parameter_fit import production_trial

    season, params = study_season(2025), parameters()
    cutoff = season.players.as_of - timedelta(days=5)
    players = season.players.model_copy(
        update={
            "boxes": tuple(
                b.model_copy(
                    update={"stats": {**b.stats, "AST": b.minutes if b.played_at < cutoff else 0.0}}
                )
                for b in season.players.boxes
            )
        }
    )
    priors = season.priors.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"rates": {**p.rates, "AST": 0.0}})
                for p in season.priors.players
            )
        }
    )
    params = params.model_copy(
        update={"role_window": params.role_window.model_copy(update={"value": 2})}
    )
    row = production_trial(
        season.model_copy(update={"players": players, "priors": priors}), params, (5,)
    )
    assert row["flag_count"] == len(priors.players)
    assert row["hit_rate"] == 0.0
    assert row["selected_mae"] > row["model_mae"]


def test_overlapping_seasons_are_rejected():
    data, params = study(), parameters()
    with pytest.raises(DataError, match="training season"):
        run_study(
            data.model_copy(update={"validation": data.training}),
            params,
            digest(canonical(data)),
            digest(canonical(params)),
        )


def test_exported_raw_calibration_history_is_direct_refit_input():
    from datetime import UTC, datetime

    from fba.contracts.inseason_results import CalibrationHistory, CalibrationObservationRecord
    from fba.inseason.review import refit_history

    at = datetime(2026, 1, 1, tzinfo=UTC)
    history = CalibrationHistory(
        season_id="2025",
        snapshot_hashes=("a" * 64,),
        observations=tuple(
            CalibrationObservationRecord(
                prediction_id=str(i), category="test", created_at=at, raw_probability=p, observed=y
            )
            for i, (p, y) in enumerate(((0.9, 1.0), (0.9, 0.0), (0.1, 0.0), (0.1, 1.0)))
        ),
    )
    report = refit_history(history, "b" * 64)
    assert report["calibration"]["value"] == pytest.approx(0)
    assert report["training_brier"] == pytest.approx(0.25)
    assert report["holdout_passed"] is False
