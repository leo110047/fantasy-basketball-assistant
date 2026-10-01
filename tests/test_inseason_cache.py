"""Cached projections and forecasts must equal independent cold calculations."""

from datetime import UTC, datetime, timedelta
from math import fsum
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from inseason_support import fixture, simulation
from test_inseason_projection import entry

from fba.contracts.base import DataError
from fba.contracts.config import Ratio
from fba.contracts.inseason import AdjustmentLedger, CalculationTimeout
from fba.contracts.inseason_results import RosterMove
from fba.core.lineups import best_lineup
from fba.formulas.categories import category_values
from fba.inseason import season
from fba.inseason.matchup import Simulation
from fba.inseason.projection import effective_projection
from fba.inseason.recommendations import changed_simulation, evaluate_plan
from fba.inseason.season import playoff_probability, remaining_weeks, season_forecasts, season_value


def test_projection_profiles_match_cold_return_expiry_and_back_to_back_dates():
    args = list(fixture())
    now = args[-1]
    args[2] = args[2].model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"status": "INJ", "return_on": now.date() + timedelta(days=2)})
                if p.id == "p0"
                else p
                for p in args[2].players
            ),
            "games": tuple(
                g.model_copy(update={"tipoff": now + timedelta(days=i, hours=12)})
                for i, g in enumerate(args[2].games)
            ),
        }
    )
    adjustment = entry(args).model_copy(update={"starts_on": now.date() + timedelta(days=1)})
    b2b = entry(args).model_copy(
        update={
            "id": "b2b",
            "field": next(f.id for f in args[1].fields if f.only_back_to_back),
            "value": "rest",
        }
    )
    args[4] = AdjustmentLedger(format_version=1, entries=(adjustment, b2b))
    sim = Simulation(*args)
    for day in range(8):
        on = now.date() + timedelta(days=day)
        cold = effective_projection(args[2], args[3], args[4], args[0], args[1], now, on)
        assert sim.projection(on) == cold
    assert len(sim.projection_profiles) < len(sim.projections)


def test_scenario_cache_eviction_recomputes_the_identical_forecast():
    args = list(fixture())
    args[1] = args[1].model_copy(
        update={
            "scenario_cache_entries": args[1].scenario_cache_entries.model_copy(update={"value": 2})
        }
    )
    sim = Simulation(*args)
    expected = sim.week("team0", "team1", "2")
    for roster in (("p0", "p1"), ("p0", "p2"), ("p1", "p2")):
        assert sim.week("team0", "team1", "2", {"team0": roster}) == Simulation(*args).week(
            "team0", "team1", "2", {"team0": roster}
        )
        assert all(
            len(cache) <= 2 for cache in (sim.team_cache, sim.matchup_cache, sim.forecast_cache)
        )
    assert sim.week("team0", "team1", "2") == expected


def test_scalar_season_cache_preserves_cold_values_after_eviction_and_playoff_selection():
    args = list(fixture())
    args[1] = args[1].model_copy(
        update={
            "scenario_cache_entries": args[1].scenario_cache_entries.model_copy(update={"value": 2})
        }
    )
    sim = Simulation(*args)
    for playoffs in (False, True):
        for roster in (("p0", "p1", "p2"), ("p0", "p1", "p6"), ("p0", "p1", "p8")):
            changed = {"team0": roster}
            expected = fsum(
                w.score
                for w in season_forecasts(
                    Simulation(*args), "team0", changed, include_playoffs=playoffs
                )
            )
            assert season_value(sim, "team0", changed, include_playoffs=playoffs) == expected
            assert len(sim.season().season_score_cache) <= 2
    assert season_value(sim, "team0") == fsum(
        w.score for w in season_forecasts(Simulation(*args), "team0")
    )


def test_roster_permutations_have_identical_cold_and_warmed_forecast_payloads():
    args = list(fixture())
    args[1] = args[1].model_copy(
        update={
            "weekly_exact_candidates": args[1].weekly_exact_candidates.model_copy(
                update={"value": 1}
            )
        }
    )
    a, b = {"team0": ("p6", "p0", "p1")}, {"team0": ("p1", "p6", "p0")}
    expected = Simulation(*args).week("team0", "team1", "2", a)
    sim = Simulation(*args)
    assert sim.week("team0", "team1", "2", b) == expected
    assert sim.week("team0", "team1", "2", a) == expected
    assert len(sim.matchup_cache) == 2  # One changed and one no-move baseline.


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("playoffs", (False, True))
def test_season_score_reuses_only_actual_remaining_opponent_context(monkeypatch, mode, playoffs):
    args = list(fixture(mode=mode, teams=4))
    sim = Simulation(*args).season()
    original = season.forecast_score
    calls = []

    def counted(*values):
        calls.append(values[3])
        return original(*values)

    monkeypatch.setattr(season, "forecast_score", counted)
    own = {"team0": ("p0", "p1", "p12")}
    expected = season_value(sim, "team0", own, include_playoffs=playoffs)
    assert calls == list(remaining_weeks(sim, include_playoffs=playoffs))
    calls.clear()
    unrelated = {**own, "team2": ("p6", "p7", "p13")}
    assert season_value(sim, "team0", unrelated, include_playoffs=playoffs) == expected
    assert not calls  # A non-opponent's trade does not alter this team's forecast.
    actual_opponent = {**own, "team1": ("p3", "p4", "p14")}
    value = season_value(sim, "team0", actual_opponent, include_playoffs=playoffs)
    assert calls == list(remaining_weeks(sim, include_playoffs=playoffs))
    assert value == fsum(
        w.score
        for w in season_forecasts(
            Simulation(*args), "team0", actual_opponent, include_playoffs=playoffs
        )
    )


def test_season_score_key_distinguishes_week_order_with_the_same_opponent_set(monkeypatch):
    sim = simulation(teams=4).season()
    weeks = remaining_weeks(sim)
    original_pairs = sim.snapshot.pairings
    original = season.forecast_score
    calls = []

    def counted(*values):
        calls.append((values[3], values[2]))
        return original(*values)

    monkeypatch.setattr(season, "forecast_score", counted)
    for swapped_week in weeks:
        sim.snapshot = sim.snapshot.model_copy(
            update={
                "pairings": tuple(
                    pair.model_copy(
                        update={
                            "home": "team0" if pair.home == "team0" else "team1",
                            "away": "team2" if pair.home == "team0" else "team3",
                        }
                    )
                    if pair.week_id == swapped_week
                    else pair
                    for pair in original_pairs
                )
            }
        )
        calls.clear()
        season_value(sim, "team0")
        assert len(calls) == len(weeks)


def test_cached_season_score_still_rejects_a_missing_remaining_opponent():
    sim = simulation().season()
    season_value(sim, "team0")
    last = remaining_weeks(sim)[-1]
    sim.snapshot = sim.snapshot.model_copy(
        update={"pairings": tuple(p for p in sim.snapshot.pairings if p.week_id != last)}
    )
    with pytest.raises(season.MissingSeasonOpponent, match="actual remaining opponent is missing"):
        season_value(sim, "team0")


def complete_expected_assignment(sim, slots, positions, means, fixed=()):
    def score(ids):
        box = sum((means[p] for p in (*fixed, *ids)), start=np.zeros(len(sim.axes)))
        return float(category_values(box, sim.league.categories, sim.axes).sum())

    return best_lineup(slots, positions, score, sim.params.tolerance.value)[0]


@pytest.mark.parametrize("seed", range(8))
def test_expected_assignment_reuse_matches_all_signed_legal_choices(seed):
    sim = simulation()
    rng = np.random.default_rng(seed)
    means = {f"p{i}": rng.uniform(-4, 30, len(sim.axes)) for i in range(5)}
    positions = {p: ("PG", "C") for p in means}
    slots = sim.league.starter_slots
    expected = complete_expected_assignment(sim, slots, positions, means)
    actual = sim.expected_assignment(slots, positions, means, ())
    assert actual == expected
    actual.clear()
    assert (
        sim.expected_assignment(slots, dict(reversed(list(positions.items()))), means, ())
        == expected
    )
    assert len(sim.expected_cache) == 1


def test_expected_assignment_reuse_invalidates_mean_fixed_positions_and_slot_changes(monkeypatch):
    from fba.inseason import matchup

    sim = simulation()
    sim.params = sim.params.model_copy(
        update={
            "scenario_cache_entries": sim.params.scenario_cache_entries.model_copy(
                update={"value": 2}
            )
        }
    )
    means = {p: np.ones(len(sim.axes)) for p in ("p0", "p1", "p2")}
    positions, fixed = {"p0": ("PG", "C"), "p1": ("PG", "C")}, ("p2",)
    slots = sim.league.starter_slots
    original = matchup.best_lineup
    calls = []

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(matchup, "best_lineup", counted)
    for change in (None, "fixed", "movable", "positions", "slot"):
        if change == "fixed":
            means["p2"][sim.axes.index("FGM")] += 10
        elif change == "movable":
            means["p0"][sim.axes.index("REB")] += 10
        elif change == "positions":
            positions["p0"] = ("C",)
        elif change == "slot":
            slots = (slots[0].model_copy(update={"eligible_positions": ("C",)}), *slots[1:])
        before = len(calls)
        expected = complete_expected_assignment(sim, slots, positions, means, fixed)
        assert sim.expected_assignment(slots, positions, means, fixed) == expected
        assert len(calls) == before + 1 and len(sim.expected_cache) <= 2
        assert sim.expected_assignment(slots, positions, means, fixed) == expected
        assert len(calls) == before + 1


def test_expected_assignment_cache_keeps_errors_and_cancellation_explicit():
    sim = simulation()
    assert sim.expected_assignment(sim.league.starter_slots, {}, {}, ()) == {}
    sim.cancelled = lambda: True
    with pytest.raises(CalculationTimeout, match="cancelled"):
        sim.expected_assignment(sim.league.starter_slots, {}, {}, ())
    other = simulation()
    other.league = other.league.model_copy(
        update={
            "categories": tuple(
                c.model_copy(
                    update={"formula": c.formula.model_copy(update={"zero_denominator": "error"})}
                )
                if isinstance(c.formula, Ratio)
                else c
                for c in other.league.categories
            )
        }
    )
    for _ in range(2):
        with pytest.raises(DataError, match="zero denominator"):
            other.expected_assignment(other.league.starter_slots, {}, {}, ())
        assert not other.expected_cache


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
def test_week_forecasts_equal_uncached_exhaustive_initial_assignments(monkeypatch, mode):
    args = fixture(mode=mode, teams=4)
    sim = Simulation(*args)
    changed = {"team0": ("p0", "p1", "p12")}
    actual = season_forecasts(sim, "team0", changed, include_playoffs=True)
    assert sim.season().expected_cache
    monkeypatch.setattr(Simulation, "expected_assignment", complete_expected_assignment)
    assert actual == season_forecasts(Simulation(*args), "team0", changed, include_playoffs=True)


def complete_playoff_probability(sim, changed):
    """The former whole-league per-sample sorting, without point/result reuse."""
    engine = sim.season()
    teams = sorted(engine.snapshot.teams, key=lambda t: t.id)
    scores = {
        t.id: np.full(engine.samples, t.wins + engine.league.week_tie_value * t.ties) for t in teams
    }
    for pair in engine.snapshot.pairings:
        if pair.week_id not in remaining_weeks(engine):
            continue
        a, b, _ = engine.matchup_totals(pair.home, pair.away, pair.week_id, changed)
        scores[pair.home] += engine.score(a, b, standings=True)[1]
        scores[pair.away] += engine.score(b, a, standings=True)[1]
    hits = dict.fromkeys(scores, 0)
    seeds = {t.id: t.seed for t in teams}
    for sample in range(engine.samples):
        order = sorted(
            scores,
            key=lambda tid: (
                -round(float(scores[tid][sample]) / engine.params.tolerance.value),
                seeds[tid],
                tid,
            ),
        )
        for tid in order[: engine.league.playoff_teams]:
            hits[tid] += 1
    return {tid: n / engine.samples for tid, n in hits.items()}


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
def test_playoff_point_reuse_equals_complete_league_simulation_and_preserves_arrays(mode):
    args = list(fixture(mode=mode, teams=4))
    args[5] = args[5].model_copy(
        update={"teams": tuple(t.model_copy(update={"seed": 1}) for t in args[5].teams)}
    )
    sim = Simulation(*args)
    baseline = playoff_probability(sim)
    assert baseline == complete_playoff_probability(Simulation(*args), {})
    saved = {k: (a.copy(), b.copy()) for k, (a, b) in sim.season().standings_point_cache.items()}
    changed = {"team0": ("p3", "p1", "p2"), "team1": ("p0", "p4", "p5")}
    assert playoff_probability(sim, changed) == complete_playoff_probability(
        Simulation(*args), changed
    )
    for key, (a, b) in saved.items():
        actual = sim.season().standings_point_cache[key]
        np.testing.assert_array_equal(a, actual[0])
        np.testing.assert_array_equal(b, actual[1])
    baseline["team0"] = -1
    assert playoff_probability(sim) == complete_playoff_probability(Simulation(*args), {})


def test_schedule_lookup_equals_full_scan_for_both_teams_and_preserves_draws():
    sim = Simulation(*fixture())
    on = sim.as_of.astimezone(sim.zone).date()
    for day in range(16):
        when = on + timedelta(days=day)
        for team in ("NBA0", "VISIT0", "NBA1", "missing"):
            expected = tuple(
                g
                for g in sim.games
                if team in (g.home, g.away) and g.tipoff.astimezone(sim.zone).date() == when
            )
            assert sim.games_on(team, when) == expected
        actual = sim.daily_draws(("p0",), when, sim.as_of)
        expected_games = tuple(
            g
            for g in sim.games
            if "NBA0" in (g.home, g.away)
            and g.tipoff > sim.as_of
            and g.tipoff.astimezone(sim.zone).date() == when
        )
        if expected_games:
            expected_draws = sum(sim.game_draw("p0", g) for g in expected_games)
            np.testing.assert_array_equal(actual["p0"], expected_draws)
        else:
            assert not actual
    assert sum(len(games) for games in sim.schedule_index.values()) == 2 * len(sim.games)


def test_schedule_lookup_invalidates_replaced_schedule_and_timezone_across_dst():
    sim = Simulation(*fixture())
    first = sim.games[0].model_copy(
        update={"id": "first-fold", "tipoff": datetime(2026, 11, 1, 5, 30, tzinfo=UTC)}
    )
    second = first.model_copy(
        update={"id": "second-fold", "tipoff": datetime(2026, 11, 1, 6, 30, tzinfo=UTC)}
    )
    sim.games = (second, first)
    on = first.tipoff.astimezone(sim.zone).date()
    assert sim.games_on(first.home, on) == (second, first)
    changed = first.model_copy(update={"tipoff": first.tipoff + timedelta(days=1)})
    sim.games = (changed,)
    assert sim.games_on(first.home, on) == ()
    assert sim.games_on(first.away, on + timedelta(days=1)) == (changed,)
    sim.zone = ZoneInfo("Pacific/Honolulu")
    assert sim.games_on(first.home, on) == (changed,)
    assert sim.games_on(first.home, on + timedelta(days=1)) == ()


def test_changed_rosters_reuse_ros_draws_but_keep_transition_forecasts_private():
    args = fixture()
    sim = Simulation(*args)
    game = next(g for g in sim.games if g.home == "NBA0")
    weekly = sim.game_draw("p0", game)
    season = sim.season()
    ros = season.game_draw("p0", game)
    move = RosterMove(
        add="p6", drop="p2", effective_on=sim.as_of.astimezone(sim.zone).date(), starter_games=0
    )
    changed = changed_simulation(sim, "team0", (move,))
    assert changed.draws is sim.draws
    assert changed.season().draws is season.draws
    assert changed.season().game_draw("p0", game) is ros
    assert weekly.shape[0] == sim.params.simulations.value
    assert ros.shape[0] == sim.params.season_simulations.value
    assert changed.season().team_cache is not season.team_cache
    assert changed.season().forecast_cache is not season.forecast_cache
    assert not sim.transitions and not season.transitions
    cold = changed_simulation(Simulation(*args), "team0", (move,))
    assert changed.season().week("team0", "team1", "3") == cold.season().week("team0", "team1", "3")
    assert not sim.transitions and not season.transitions


@pytest.mark.parametrize("case", ("before_future", "later_transition", "injury_unknown"))
def test_plan_future_value_equals_full_transition_timeline(case):
    from inseason_support import DEFAULTS

    from fba.contracts.config import InjurySlot
    from fba.contracts.inseason import InseasonPreferences

    args = list(fixture())
    if case == "injury_unknown":
        args[0] = args[0].model_copy(
            update={
                "injury_slots": (
                    InjurySlot(id="IL", label="IL", count=1, eligible_statuses=("INJ",)),
                )
            }
        )
        args[2] = args[2].model_copy(
            update={
                "players": tuple(
                    p.model_copy(update={"status": "INJ"}) if p.id == "p7" else p
                    for p in args[2].players
                )
            }
        )
        args[5] = args[5].model_copy(
            update={
                "teams": tuple(
                    t.model_copy(update={"injury_players": {"p7": "IL"}}) if t.id == "team0" else t
                    for t in args[5].teams
                )
            }
        )
    sim = Simulation(*args)
    week = next(w for w in sim.league.matchups if w.id == "2")
    on = week.end + timedelta(days=3) if case == "later_transition" else week.end
    moves = (RosterMove(add="p6", drop="p0", effective_on=on, starter_games=0),)
    before = sim.week("team0", "team1", week.id)
    start = week.end + timedelta(days=1)
    baseline = season_value(sim, "team0", after=start)
    direct = changed_simulation(sim, "team0", moves)
    regular = season_value(direct, "team0", after=start) - baseline
    whole = season_value(direct, "team0", after=start, include_playoffs=True) - season_value(
        sim, "team0", after=start, include_playoffs=True
    )
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    result = evaluate_plan(sim, prefs, moves, before, baseline)
    assert result.delta_season == regular
    assert result.delta_strength == whole
    assert not sim.transitions


def test_projection_lookup_reuses_immutable_profiles_and_observes_replaced_profile():
    sim = simulation()
    on = sim.as_of.astimezone(sim.zone).date()
    original = sim.projection(on)
    lookup = sim.player_index.get(original)
    later = original.model_copy(update={"on": on + timedelta(days=1)})
    assert sim.player_index.get(later) is lookup
    changed = original.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"player": p.player.model_copy(update={"team_id": "NBA2"})})
                if p.player.id == "p0"
                else p
                for p in original.players
            )
        }
    )
    sim.projections[on] = changed
    new_lookup = sim.player_index.get(sim.projection(on))
    assert new_lookup["p0"].player.team_id == "NBA2"
    assert lookup["p0"].player.team_id == "NBA0"


@pytest.mark.parametrize("samples", (40, 200))
def test_daily_one_and_multiple_games_keep_draw_values_and_cache_independence(samples):
    sim = Simulation(*fixture(), samples=samples)
    game = next(g for g in sim.games if g.home == "NBA0" and g.tipoff > sim.as_of)
    on = game.tipoff.astimezone(sim.zone).date()
    games = sim.games_on("NBA0", on)
    assert len(games) == 1
    draw = np.arange(sim.samples * len(sim.axes), dtype=np.float64).reshape(
        sim.samples, len(sim.axes)
    )
    draw[0, 0] = -0.0
    sim.draws["p0", game.id] = draw
    expected = np.zeros_like(draw) + draw
    actual = sim.daily_draws(("p0",), on, sim.as_of)["p0"]
    assert np.array_equal(actual, expected)
    assert np.array_equal(np.signbit(actual), np.signbit(expected))
    actual.fill(-1)
    assert np.array_equal(sim.draws["p0", game.id], draw) and draw[0, 1] == 1
    second = game.model_copy(update={"id": game.id + ":second"})
    sim.games = (*sim.games, second)
    other = np.ones_like(draw) * 2
    sim.draws["p0", second.id] = other
    assert np.array_equal(sim.daily_draws(("p0",), on, sim.as_of)["p0"], expected + other)
