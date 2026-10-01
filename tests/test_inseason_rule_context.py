"""League-only edits must not reuse decisions or relabel archived forecasts."""

import pytest
from inseason_support import simulation
from test_inseason_acceptance_gaps import prepared_session
from test_inseason_review import final_scores, prediction
from test_inseason_service import selected_session

from fba.contracts.base import DataError
from fba.contracts.inseason_results import AddPlan, PredictionRecord
from fba.data.codec import canonical, digest
from fba.inseason.forecast_records import record_forecast
from fba.inseason.operations import latest_plan, recorded_plan
from fba.inseason.review import weekly_review
from fba.inseason.session import json_value, snapshot_record


def test_rule_only_edit_records_a_distinct_forecast_even_when_numbers_are_equal(
    tmp_path, monkeypatch
):
    session, now = prepared_session(tmp_path)
    sim = session.simulation(as_of=now, require_league=False)
    forecast = sim.week("team0", "team1", "2")
    monkeypatch.setattr(session, "simulation", lambda: sim)
    monkeypatch.setattr(sim, "week", lambda *args: forecast)
    record_forecast(session, "2")
    store = session.league_store()
    first = store.history("predictions")
    assert len(first) == 1
    sim.league = sim.league.model_copy(update={"bench_slots": sim.league.bench_slots + 1})
    session.save_state(session.state().model_copy(update={"league": sim.league}))
    record_forecast(session, "2")
    rows = store.history("predictions")
    assert len(rows) == 2 and rows[0] == first[0]
    records = tuple(snapshot_record(row, PredictionRecord) for row in rows)
    assert records[0].with_adjustments == records[1].with_adjustments
    assert records[0].league_sha256 != records[1].league_sha256
    assert records[1].league_sha256 == digest(canonical(sim.league))


@pytest.mark.parametrize("change", ("rules", "legacy"))
def test_recorded_plan_is_rejected_after_rule_change_or_without_rule_provenance(tmp_path, change):
    sim = simulation()
    session = selected_session(tmp_path)
    session.params = sim.params
    state = session.state().model_copy(update={"normalized_sha256": "a" * 64})
    session.save_state(state)
    record = prediction(sim)
    plan = AddPlan(
        id="rules-saved",
        moves=(),
        before=record.with_adjustments,
        after=record.with_adjustments,
        delta_week=0.1,
        delta_season=0.0,
        delta_strength=0.0,
        score=0.1,
        traces=(),
    )
    record = record.model_copy(update={"recommendations": (plan,)})
    if change == "legacy":
        record = PredictionRecord.model_validate(record.model_dump(exclude={"league_sha256"}))
    session.league_store().append_snapshot(
        "predictions", "synthetic rules", sim.as_of, json_value(record.model_dump(mode="json"))
    )
    if change == "rules":
        assert latest_plan(session, "2") == plan
        changed = sim.league.model_copy(update={"week_tie_value": 0.25})
        session.save_state(state.model_copy(update={"league": changed}))
    assert latest_plan(session, "2") is None
    with pytest.raises(DataError, match="重新計算 F3"):
        recorded_plan(session, plan.id)
    assert len(session.league_store().history("predictions")) == 1


@pytest.mark.parametrize(
    "field,value", (("scoring", "h2h_each_category"), ("week_tie_value", 0.25))
)
def test_review_rejects_changed_rules_despite_identical_category_ids(field, value):
    sim = simulation()
    saved = prediction(sim)
    changed = sim.league.model_copy(update={field: value})
    assert [c.id for c in changed.categories] == [c.id for c in sim.league.categories]
    with pytest.raises(DataError, match="archived league rules"):
        weekly_review(changed, sim.params, final_scores(sim), (saved,), "2", {})


def test_review_does_not_rescore_hidden_later_decisions_with_changed_rules():
    sim = simulation()
    saved = prediction(sim)
    changed = saved.model_copy(update={"id": "later-rules", "league_sha256": "b" * 64})
    with pytest.raises(DataError, match="archived league rules"):
        weekly_review(sim.league, sim.params, final_scores(sim), (saved, changed), "2", {})
