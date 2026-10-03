from datetime import timedelta

import numpy as np
import pytest
from inseason_support import fixture
from test_inseason_projection import entry, project

from fba.formulas.arrays import evaluate_array
from fba.inseason.adjustments import redistribute


@pytest.mark.parametrize("target", [0.83, 0.97, 1.10])
def test_scaled_low_counts_preserve_target_mean(target):
    result = evaluate_array(
        "bootstrap_scale",
        history=np.ones((10, 1)),
        samples=np.ones((100, 1)),
        target=np.array([target]),
        zero_mean_draws=np.zeros((100, 1)),
    ).result
    assert result.mean() == pytest.approx(target)


def test_zero_minute_games_affect_minutes_and_role_flags():
    args = list(fixture())
    player = args[2].players[0]
    baseline = project(args).players[0]
    boxes = tuple(
        b.model_copy(update={"minutes": 0, "stats": dict.fromkeys(b.stats, 0)})
        if b.player_id == player.id
        else b
        for b in args[2].boxes
    )
    args[2] = args[2].model_copy(update={"boxes": boxes})
    result = project(args).players[0]
    assert result.observed_minutes and set(result.observed_minutes) == {0}
    # Explicit DNPs reduce appearances, rather than cutting conditional minutes
    # and then counting the same nonappearance a second time.
    assert result.minutes == pytest.approx(baseline.minutes)
    assert result.probability == pytest.approx(3 / 13)
    assert result.traces["expected:minutes"].result < baseline.traces["expected:minutes"].result
    assert any(f.kind == "role" for f in result.flags)


def test_expected_return_date_restores_future_availability():
    args = list(fixture())
    on = args[-1].date() + timedelta(days=2)
    injured = args[2].players[0].model_copy(update={"status": "O", "return_on": on})
    args[2] = args[2].model_copy(update={"players": (injured, *args[2].players[1:])})
    assert project(args, on=on - timedelta(days=1)).players[0].probability == 0
    assert project(args, on=on).players[0].probability == args[1].availability["healthy"].value


def test_back_to_back_adjustment_applies_after_general_status():
    from fba.inseason.projection import adjusted_values

    args = fixture()
    base = entry(args)
    entries = (
        base.model_copy(update={"field": "back_to_back", "value": "rest"}),
        base.model_copy(update={"id": "status", "field": "status", "value": "healthy"}),
    )
    assert adjusted_values(30, 1, {}, {}, entries, args[1], True)[1] == 0
    assert adjusted_values(30, 1, {}, {}, entries, args[1], False)[1] == 1


def test_redistribution_does_not_take_minutes_from_unavailable_teammates():
    projection = project(fixture())
    target = projection.players[0]
    teammate = projection.players[1]
    projection = projection.model_copy(
        update={
            "players": tuple(
                p.model_copy(
                    update={
                        "player": p.player.model_copy(update={"team_id": target.player.team_id}),
                        "probability": 0 if p.player.id == teammate.player.id else 1,
                    }
                )
                for p in projection.players
            )
        }
    )
    changed = redistribute(projection, target.player.id, target.minutes + 1)
    assert teammate.player.id not in changed


def test_uncalibrated_report_does_not_disable_recommendations(monkeypatch):
    from fba.inseason import operations

    class Session:
        def simulation(self):
            return "valid simulation"

    monkeypatch.setattr(operations, "calibration_status", lambda session: {"enabled": False})
    monkeypatch.setattr(operations, "search_adds", lambda *args: ())
    monkeypatch.setattr(operations, "week_result", lambda *args: None)
    session = Session()
    session.preferences = None
    assert operations.recommendations(session, "2") == ()


def test_roster_may_leave_starter_positions_unfilled():
    from inseason_support import simulation

    from fba.inseason.recommendations import legal_roster

    sim = simulation()
    at = sim.as_of.astimezone(sim.zone).date()
    guards = tuple(p.id for p in sim.players.players if p.positions == ("PG",))
    capacity = len(sim.league.starter_slots) + sim.league.bench_slots
    assert len(guards) >= capacity
    assert legal_roster(sim, guards[:capacity], at)
    assert not legal_roster(sim, (guards[0],) * capacity, at)


def test_week_win_probability_excludes_ties_but_standings_keep_tie_credit():
    from inseason_support import simulation

    sim = simulation()
    totals = np.ones((sim.samples, len(sim.axes)))
    assert np.all(sim.score(totals, totals)[1] == 0)
    assert np.all(sim.score(totals, totals, standings=True)[1] == sim.league.week_tie_value)


def test_no_key_categories_still_searches_legal_moves(monkeypatch):
    from inseason_support import DEFAULTS, simulation

    from fba.contracts.inseason import InseasonPreferences
    from fba.inseason import recommendations

    sim = simulation()
    before = sim.week("team0", "team1", "2")
    before = before.model_copy(
        update={
            "categories": tuple(
                c.model_copy(update={"strategy": "safe"}) for c in before.categories
            )
        }
    )
    monkeypatch.setattr(sim, "week", lambda *args: before)
    monkeypatch.setattr(recommendations, "season_value", lambda *args, **kwargs: 0)
    categories = []

    def candidates(*args, **kwargs):
        categories.append(args[-1])
        return []

    monkeypatch.setattr(recommendations, "candidate_moves", candidates)
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    recommendations.search_add_plans(sim, prefs, "2", lambda _: None)
    assert categories
    assert all(ids == tuple(c.id for c in before.categories) for ids in categories)


def test_multi_position_fallback_uses_unique_peers_across_groups():
    from fba.contracts.inseason import PriorGroup
    from fba.inseason.projection import fallback_prior

    args = fixture()
    player = args[2].players[2]
    params = args[1].model_copy(
        update={
            "prior_groups": tuple(
                PriorGroup(
                    id=position, positions=(position,), minimum_minutes=0, maximum_minutes=60
                )
                for position in ("PG", "C")
            )
        }
    )
    priors = args[3].model_copy(
        update={"players": tuple(p for p in args[3].players if p.player_id != player.id)}
    )
    result = fallback_prior(player, (), args[2].players, priors, params)
    assert result.minutes == pytest.approx(np.mean([p.minutes for p in priors.players]))
    for stat, rate in result.rates.items():
        assert rate == pytest.approx(np.mean([p.rates[stat] for p in priors.players]))


def test_bootstrap_preserves_equal_shooting_percentages_between_players():
    from inseason_support import simulation

    sim = simulation()
    game = next(g for g in sim.games if "NBA0" in (g.home, g.away) and g.tipoff > sim.as_of)
    draws = sim.game_draw("p0", game)
    for shot in sim.league.shots:
        made, attempted = (sim.axes.index(s) for s in (shot.made, shot.attempted))
        expected = next(
            p for p in sim.projection(game.tipoff.date()).players if p.player.id == "p0"
        )
        active = draws[:, attempted] > 0
        np.testing.assert_allclose(
            draws[active, made] / draws[active, attempted], expected.probabilities[shot.id]
        )


def test_opponent_baseline_preserves_locked_starters_and_bench():
    from inseason_support import simulation

    sim = simulation()
    sim.league = sim.league.model_copy(update={"lineup_lock": "daily"})
    on = sim.as_of.astimezone(sim.zone).date()
    opponent = sim.snapshot.teams[1]
    _, lineups = sim.total(opponent.id, "2")
    today = next(day for day in lineups if day.on == on)
    assert today.slots == opponent.selected_slots
    assert today.bench == ("p5",)


@pytest.mark.parametrize("return_offset", [-1, 0])
def test_overdue_return_estimate_does_not_override_newest_out_status(return_offset):
    args = list(fixture())
    today = args[-1].date()
    player = (
        args[2]
        .players[0]
        .model_copy(
            update={
                "status": "O",
                "known_at": args[-1],
                "return_on": today + timedelta(days=return_offset),
            }
        )
    )
    args[2] = args[2].model_copy(update={"players": (player, *args[2].players[1:])})
    assert project(args, on=today).players[0].probability == args[1].availability["O"].value
    assert (
        project(args, on=today + timedelta(days=1)).players[0].probability
        == args[1].availability["O"].value
    )
