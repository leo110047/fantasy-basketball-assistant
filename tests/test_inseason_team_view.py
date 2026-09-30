from datetime import timedelta

from inseason_support import fixture, simulation
from test_inseason_projection import entry, project

from fba.contracts.inseason import AdjustmentLedger
from fba.formulas.registry import evaluate
from fba.inseason.team_view import team_views


def test_team_summary_counts_league_dates_and_uses_registered_equations():
    sim = simulation()
    on = sim.as_of.astimezone(sim.zone).date()
    projection = sim.projection(on)
    rows = team_views(projection, sim.league, sim.params, sim.games, sim.league, sim.ledger)
    row = next(r for r in rows if r.team_id == "NBA0")
    current = next(w for w in sim.league.matchups if w.start <= on <= w.end)
    dates = [g.tipoff.astimezone(sim.zone).date() for g in sim.games if g.home == "NBA0"]
    assert row.week_games == sum(current.start <= d <= current.end for d in dates)
    assert row.back_to_back == tuple(d for d in dates if d >= on and d - timedelta(days=1) in dates)
    for trace in (row.minutes, row.budget, row.difference, *row.players[0].recent_minutes.values()):
        assert evaluate(trace.formula_id, **trace.inputs).result == trace.result
    offline = team_views(projection, sim.league, sim.params, sim.games, None, sim.ledger)
    assert all(t.week_games is None and t.next_week_games is None for t in offline)


def test_grouped_return_date_changes_status_on_exact_day_and_undo_restores():
    data = list(fixture())
    start = data[-1].date()
    returning = start + timedelta(days=2)
    original = project(data, on=returning)
    out = entry(data).model_copy(
        update={"field": "status", "value": "INJ", "ends_on": returning - timedelta(days=1)}
    )
    back = out.model_copy(
        update={
            "id": "return",
            "starts_on": returning,
            "ends_on": data[0].ends_on,
            "value": "healthy",
        }
    )
    ledger = AdjustmentLedger(format_version=1, entries=(out, back))
    projected = project(data, ledger=ledger, on=start)
    summary = next(
        t
        for t in team_views(projected, data[0], data[1], data[2].games, data[0], ledger)
        if t.team_id == "NBA0"
    )
    assert summary.players[0].manual_status == "INJ"
    assert summary.players[0].manual_return_on == returning
    assert (
        project(data, ledger=ledger, on=returning - timedelta(days=1)).players[0].probability == 0
    )
    assert project(data, ledger=ledger, on=returning).players[0].probability == 1
    revoke = back.model_copy(
        update={"id": "undo", "revokes": (out.id, back.id), "created_at": data[-1]}
    )
    restored = project(
        data, ledger=ledger.model_copy(update={"entries": (*ledger.entries, revoke)}), on=returning
    )
    assert restored == original
