from datetime import timedelta

from inseason_support import simulation
from test_inseason_service import selected_session

from fba.contracts.inseason_results import PredictionRecord
from fba.data.codec import canonical, digest
from fba.inseason.operations import complete_reviews
from fba.inseason.review import (
    calibration_history,
    cumulative_review,
    latest_reviews,
    weekly_review,
)
from fba.inseason.session import json_value


def prediction(sim):
    forecast = sim.week("team0", "team1", "2")
    return PredictionRecord(
        id="recorded-before-outcome",
        created_at=sim.as_of,
        week_id="2",
        parameter_version=sim.params.version,
        parameter_sha256=digest(canonical(sim.params)),
        input_hashes=("a" * 64,),
        ledger_sha256=digest(canonical(sim.ledger)),
        with_adjustments=forecast,
        without_adjustments=forecast,
        recommendations=(),
        proposal_probabilities={},
        week_score_kind="win_probability",
    )


def final_scores(sim, *, reverse=False):
    return sim.snapshot.model_copy(
        update={
            "actual": tuple(
                row.model_copy(
                    update={
                        "final": True,
                        "totals": {
                            key: (i + 1) * (10 if (row.team_id == "team0") != reverse else 1)
                            for i, key in enumerate(row.totals)
                        },
                    }
                )
                for row in sim.snapshot.actual
            ),
        }
    )


def test_official_score_correction_uses_latest_report_without_changing_prediction():
    sim = simulation()
    saved = prediction(sim)
    original = canonical(saved)
    first = weekly_review(sim.league, sim.params, final_scores(sim), (saved,), "2", {})
    correction = weekly_review(
        sim.league, sim.params, final_scores(sim, reverse=True), (saved,), "2", {}
    )
    assert canonical(first) != canonical(correction)
    assert latest_reviews((first, correction)) == (correction,)
    assert cumulative_review((first, correction), sim.params) == cumulative_review(
        (correction,), sim.params
    )
    history = calibration_history(sim.league.season_id, (saved,), (first, correction), ("a" * 64,))
    assert len(history.observations) == len(sim.league.categories)
    assert [row.observed for row in history.observations] == [
        row["observed"] for row in sorted(correction.rows, key=lambda row: row["category"])
    ]
    assert canonical(saved) == original


def test_sync_appends_only_changed_weekly_reports_and_preserves_history(tmp_path, monkeypatch):
    session = selected_session(tmp_path)
    sim = simulation()
    saved = prediction(sim)
    store = session.league_store()
    original = store.append_snapshot(
        "predictions", "test", sim.as_of, json_value(saved.model_dump(mode="json"))
    )
    sim.as_of += timedelta(days=3)
    sim.snapshot = final_scores(sim)
    monkeypatch.setattr(session, "simulation", lambda: sim)
    complete_reviews(session)
    first = store.history("reviews")
    assert len(first) == 1
    complete_reviews(session)
    assert store.history("reviews") == first
    sim.snapshot = final_scores(sim, reverse=True)
    complete_reviews(session)
    assert len(store.history("reviews")) == 2
    assert store.history("reviews")[0] == first[0]
    assert store.load_snapshot(original).payload == saved.model_dump(mode="json")


def test_old_tie_credit_and_new_win_probabilities_are_not_mixed():
    import pytest

    sim = simulation()
    original = prediction(sim)
    equal = sim.snapshot.model_copy(
        update={
            "actual": tuple(
                row.model_copy(update={"final": True, "totals": dict.fromkeys(row.totals, 1.0)})
                for row in sim.snapshot.actual
            )
        }
    )
    legacy = original.model_copy(
        update={
            "id": "legacy",
            "with_adjustments": original.with_adjustments.model_copy(update={"score": 0.5}),
            "without_adjustments": original.without_adjustments.model_copy(update={"score": 0.5}),
        }
    )
    # Files saved before the contract change do not contain this field.
    legacy = PredictionRecord.model_validate(legacy.model_dump(exclude={"week_score_kind"}))
    assert legacy.week_score_kind == "standings_points"
    current = original.model_copy(
        update={
            "id": "current",
            "with_adjustments": original.with_adjustments.model_copy(update={"score": 0.25}),
            "without_adjustments": original.without_adjustments.model_copy(update={"score": 0.25}),
        }
    )
    old_review = weekly_review(sim.league, sim.params, equal, (legacy,), "2", {})
    assert old_review.week_brier == 0
    combined = weekly_review(sim.league, sim.params, equal, (legacy, current), "2", {})
    assert combined.week_score_kind == "win_probability"
    assert combined.week_prediction_ids == ("current",)
    assert combined.week_brier == pytest.approx(0.25**2)
    assert len(combined.rows) == len(sim.league.categories)
    assert len({(row["category"], row["prediction"]) for row in combined.rows}) == len(
        combined.rows
    )
    history = calibration_history(sim.league.season_id, (legacy, current), (combined,), ())
    assert len(history.observations) == len(sim.league.categories)
    cumulative = cumulative_review(
        (old_review.model_copy(update={"week_id": "1"}), combined), sim.params
    )
    assert cumulative["week_brier"] == pytest.approx(0.25**2)
    assert cumulative["excluded_legacy_weeks"] == 1


def test_archived_category_axes_cannot_silently_score_different_current_rules():
    import pytest

    from fba.contracts.base import DataError

    sim = simulation()
    saved = prediction(sim)
    changed = sim.league.model_copy(update={"categories": tuple(reversed(sim.league.categories))})
    with pytest.raises(DataError, match="archived category axes"):
        weekly_review(changed, sim.params, final_scores(sim), (saved,), "2", {})
