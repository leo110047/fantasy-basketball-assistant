from datetime import timedelta

import pytest
from inseason_support import DEFAULTS, fixture, simulation
from test_inseason_projection import entry, project

from fba.contracts.inseason import AdjustmentLedger
from fba.contracts.yahoo import YahooCatalog
from fba.formulas.registry import evaluate
from fba.inseason.matchup import Simulation
from fba.inseason.team_view import category_estimate, player_categories, team_views


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


def test_player_category_means_are_conditional_and_preserve_undefined_ratios():
    data = fixture()
    projected = project(data)
    values = player_categories(projected, data[0], data[1], data[3], data[2], data[2].games)
    player = projected.players[0]
    assert values[player.player.id]["PTS"] == pytest.approx(player.expected["PTS"])
    assert values[player.player.id]["FG%"] == pytest.approx(0.5)
    ratio = next(c for c in data[0].categories if c.id == "FG%")
    assert category_estimate({"FGM": 0.0, "FGA": 0.0}, ratio) is None
    assert category_estimate({"FGM": 0.0}, ratio) is None
    out = entry(data).model_copy(update={"field": "status", "value": "INJ"})
    projected_out = project(data, ledger=AdjustmentLedger(format_version=1, entries=(out,)))
    values_out = player_categories(projected_out, data[0], data[1], data[3], data[2], data[2].games)
    assert values_out[player.player.id]["PTS"] == values[player.player.id]["PTS"]
    assert values_out[player.player.id]["FG%"] == values[player.player.id]["FG%"]
    assert projected_out.players[0].expected["PTS"] == 0


@pytest.mark.parametrize("status, probability", (("healthy", 1.0), ("GTD", 0.5), ("INJ", 0.0)))
def test_roster_means_exclude_availability_while_trade_simulation_draws_include_it(
    status, probability
):
    data = list(fixture())
    catalog = YahooCatalog.model_validate_json((DEFAULTS / "yahoo-catalog.json").read_bytes())
    data[0] = data[0].model_copy(
        update={"categories": (*data[0].categories, catalog.category_labels["DD"])}
    )
    subject_id = data[2].players[0].id
    data[2] = data[2].model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"status": status}) if p.id == subject_id else p
                for p in data[2].players
            ),
            "boxes": tuple(
                b.model_copy(update={"stats": {**b.stats, "REB": 30.0, "AST": 30.0}})
                if b.player_id == subject_id
                else b
                for b in data[2].boxes
            ),
        }
    )
    data[3] = data[3].model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"rates": {**p.rates, "REB": 1.0, "AST": 1.0}})
                if p.player_id == subject_id
                else p
                for p in data[3].players
            )
        }
    )
    sim = Simulation(*data, samples=20_000)
    projection = sim.projection(sim.as_of.astimezone(sim.zone).date())
    subject = next(p for p in projection.players if p.player.id == subject_id)
    conditional_points = (
        2 * subject.rates["FGM"] + subject.rates["3PM"] + subject.rates["FTM"]
    ) * subject.minutes
    values = player_categories(
        projection, sim.league, sim.params, sim.priors, sim.players, sim.games
    )
    assert values[subject_id]["PTS"] == pytest.approx(conditional_points)
    assert values[subject_id]["FG%"] == pytest.approx(0.5)
    assert values[subject_id]["DD"] == 1.0
    assert subject.probability == probability
    assert subject.expected["PTS"] == pytest.approx(conditional_points * probability)
    game = next(g for g in sim.games if g.home == subject.player.team_id)
    drawn = sim.game_draw(subject_id, game)
    assert drawn[:, sim.axes.index("PTS")].mean() == pytest.approx(
        conditional_points * probability, rel=0.02, abs=0.01
    )
    assert drawn[:, sim.axes.index("DD")].mean() == pytest.approx(probability, abs=0.01)


def test_dnp_only_history_without_frozen_distribution_keeps_displayable_means(
    tmp_path, monkeypatch
):
    from test_inseason_service import selected_session

    from fba.inseason.operations import bootstrap

    data = list(fixture())
    catalog = YahooCatalog.model_validate_json((DEFAULTS / "yahoo-catalog.json").read_bytes())
    data[0] = data[0].model_copy(
        update={"categories": (*data[0].categories, catalog.category_labels["DD"])}
    )
    subject_id = data[2].players[0].id
    data[2] = data[2].model_copy(
        update={
            "boxes": tuple(
                b.model_copy(update={"minutes": 0.0, "stats": dict.fromkeys(b.stats, 0.0)})
                if b.player_id == subject_id
                else b
                for b in data[2].boxes
            )
        }
    )
    assert data[3].distribution is None
    assert any(subject_id == b.player_id for b in data[2].boxes)
    projection = project(data)
    values = player_categories(projection, data[0], data[1], data[3], data[2], data[2].games)
    assert values[subject_id]["DD"] is None
    assert values[subject_id]["PTS"] == pytest.approx(14.0)
    assert values[subject_id]["FG%"] == pytest.approx(0.5)
    session = selected_session(tmp_path)
    state, store, at = session.state(), session.league_store(), data[-1]
    session.save_state(
        state.model_copy(
            update={
                "league": data[0],
                "players_sha256": store.snapshot("players", at, data[2].model_dump(mode="json")),
                "priors_sha256": store.snapshot("priors", at, data[3].model_dump(mode="json")),
                "normalized_sha256": store.snapshot("league", at, data[5].model_dump(mode="json")),
                "sync": state.sync.model_copy(
                    update={
                        "connected": True,
                        "authorization_valid": True,
                        "last_success": at,
                        "settings_pending": False,
                    }
                ),
            }
        )
    )

    class CapturedClock:
        @staticmethod
        def now(_zone):
            return at

    monkeypatch.setattr("fba.inseason.operations.datetime", CapturedClock)
    bootstrapped = bootstrap(session)
    assert "projection_error" not in bootstrapped
    assert bootstrapped["availability"]["enabled"]
    assert bootstrapped["player_categories"][subject_id]["DD"] is None
    assert bootstrapped["player_categories"][subject_id]["PTS"] == pytest.approx(14.0)


def test_double_double_is_derived_per_game_not_from_average_statistics():
    data = list(fixture())
    catalog = YahooCatalog.model_validate_json((DEFAULTS / "yahoo-catalog.json").read_bytes())
    data[0] = data[0].model_copy(update={"categories": (catalog.category_labels["DD"],)})
    player = data[3].players[0]
    data[3] = data[3].model_copy(
        update={
            "players": (
                player.model_copy(
                    update={"rates": {**dict.fromkeys(player.rates, 0.0), "REB": 0.5, "AST": 0.5}}
                ),
                *data[3].players[1:],
            )
        }
    )
    boxes = tuple(
        box.model_copy(
            update={
                "stats": {
                    **dict.fromkeys(box.stats, 0.0),
                    "REB": 30.0 if i % 2 else 0.0,
                    "AST": 0.0 if i % 2 else 30.0,
                }
            }
        )
        if box.player_id == player.player_id
        else box
        for i, box in enumerate(data[2].boxes)
    )
    data[2] = data[2].model_copy(update={"boxes": boxes})
    projected = project(data)
    subject = next(p for p in projected.players if p.player.id == player.player_id)
    assert subject.expected["REB"] >= 10 and subject.expected["AST"] >= 10
    values = player_categories(projected, data[0], data[1], data[3], data[2], data[2].games)
    assert values[player.player_id]["DD"] == 0.0
    no_schedule = player_categories(projected, data[0], data[1], data[3], data[2], ())
    assert no_schedule[player.player_id]["DD"] is None
