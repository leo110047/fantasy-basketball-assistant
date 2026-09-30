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
