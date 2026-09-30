from datetime import timedelta
from itertools import combinations

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from inseason_support import simulation

from fba.contracts.inseason_results import RosterMove
from fba.core.lineups import best_lineup, legal_assignment
from fba.data.codec import canonical
from fba.formulas.categories import category_values, derive_games
from fba.inseason.recommendations import changed_simulation
from fba.inseason.trades import evaluate_trade


def test_ratio_uses_totals_and_threshold_is_per_game():
    sim = simulation()
    box = np.zeros((2, len(sim.league.base_stats)))
    for i, (made, att) in enumerate(((1.0, 1.0), (1.0, 9.0))):
        box[i, sim.league.base_stats.index("FGM")] = made
        box[i, sim.league.base_stats.index("FGA")] = att
    derived = derive_games(box, sim.league.base_stats, sim.league.derived)
    category = next(c for c in sim.league.categories if c.id == "FG%")
    assert category_values(derived.sum(axis=0), (category,), sim.axes)[0] == 0.2
    assert derived[:, -1].sum() == 0


@pytest.mark.parametrize(
    "mode,year,teams", [("h2h_one_win", 2025, 2), ("h2h_each_category", 2026, 10)]
)
def test_two_seasons_and_different_leagues_have_repeatable_valid_forecasts(mode, year, teams):
    sim = simulation(mode=mode, year=year, teams=teams)
    forecast = sim.week("team0", "team1", "2")
    assert canonical(forecast) == canonical(sim.week("team0", "team1", "2"))
    assert all(0 <= c.probability <= 1 for c in forecast.categories)
    assert 0 <= forecast.score <= (1 if mode == "h2h_one_win" else len(sim.league.categories))
    assert all(len(set(d.slots.values())) == len(d.slots) for d in forecast.lineups)


@given(
    st.lists(
        st.floats(min_value=-10, max_value=10, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=15,
    )
)
def test_lineup_matches_exhaustive_legal_subsets(values):
    sim = simulation()
    positions = {f"p{i}": ("PG", "C") for i in range(len(values))}

    def objective(ids):
        return sum(values[int(p[1:])] for p in ids)

    assigned, score = best_lineup(sim.league.starter_slots, positions, objective, 1e-9)
    legal = [
        ids
        for count in range(3)
        for ids in combinations(positions, count)
        if legal_assignment(sim.league.starter_slots, positions, ids) is not None
    ]
    assert score == pytest.approx(max(map(objective, legal)))
    assert len(assigned) <= 2


def test_move_does_not_count_player_before_effective_day():
    sim = simulation()
    day = sim.as_of.date() + timedelta(days=2)
    move = RosterMove(add="p6", drop="p0", effective_on=day, starter_games=0)
    child = changed_simulation(sim, "team0", (move,))
    _, lineups = child.total("team0", "2")
    assert all("p6" not in d.slots.values() for d in lineups if d.on < day)
    assert all("p0" not in d.slots.values() for d in lineups if d.on >= day)


def test_trade_deltas_are_symmetric_and_common_random_numbers_reproduce():
    sim = simulation()
    trade = evaluate_trade(sim, "team0", "team1", ("p0",), ("p3",))
    reverse = evaluate_trade(sim, "team1", "team0", ("p3",), ("p0",))
    assert trade.opponent_delta == reverse.mine_delta
    assert canonical(trade) == canonical(evaluate_trade(sim, "team0", "team1", ("p0",), ("p3",)))


def test_daily_locked_positions_are_preserved():
    sim = simulation()
    sim.league = sim.league.model_copy(update={"lineup_lock": "daily"})
    result = sim.week("team0", "team1", "2")
    day = next(
        d
        for d in result.lineups
        if d.team_id == "team0" and d.on == sim.as_of.astimezone(sim.zone).date()
    )
    assert day.slots == sim.snapshot.teams[0].selected_slots


@pytest.mark.parametrize("mode", ["h2h_one_win", "h2h_each_category"])
@pytest.mark.parametrize("calibration", [0.0, 0.8, 1.0])
def test_today_uses_weekly_calibrated_score_and_uncertainty(mode, calibration):
    from fba.inseason.today import today

    sim = simulation(mode=mode)
    sim.params = sim.params.model_copy(
        update={"calibration": sim.params.calibration.model_copy(update={"value": calibration})}
    )
    forecast = sim.week("team0", "team1", "2")
    daily = today(sim, sim.as_of.astimezone(sim.zone).date(), "Asia/Taipei")
    assert daily.score_after == forecast.score
    assert forecast.standard_error == pytest.approx(forecast.traces[1].result * calibration)
    assert all(
        c.standard_error == pytest.approx(c.traces[-2].result * calibration)
        for c in forecast.categories
    )
    if calibration == 0:
        assert forecast.standard_error == 0
        assert all(row.marginal is None or row.marginal.result == 0 for row in daily.players)


def test_today_explains_bench_player_without_an_eligible_movable_slot():
    from fba.inseason.today import today

    sim = simulation()
    sim.league = sim.league.model_copy(
        update={"starter_slots": tuple(s for s in sim.league.starter_slots if s.id == "big")}
    )
    daily = today(sim, sim.as_of.astimezone(sim.zone).date(), "Asia/Taipei")
    guard = next(p for p in daily.players if p.player_id == "p0")
    assert guard.slot is None
    assert guard.marginal is None
    assert "沒有符合" in guard.reason


def test_started_player_retains_slot_without_counting_recorded_stats_twice():
    sim = simulation()
    on = sim.as_of.astimezone(sim.zone).date()
    game = next(
        g for g in sim.games if g.home == "NBA0" and g.tipoff.astimezone(sim.zone).date() == on
    )
    started = game.model_copy(
        update={"tipoff": sim.as_of - timedelta(hours=3), "status": "completed"}
    )
    sim.games = tuple(started if g.id == game.id else g for g in sim.games)
    sim.players = sim.players.model_copy(update={"games": sim.games})
    draws = sim.daily_draws(sim.roster("team0"), on, sim.as_of)
    assert not draws["p0"].any()
    forecast = sim.week("team0", "team1", "2")
    lineup = next(d for d in forecast.lineups if d.team_id == "team0" and d.on == on)
    assert lineup.slots["guard"] == "p0"
    assert "p2" not in lineup.slots.values() or lineup.slots["big"] == "p2"


def test_future_roster_snapshot_is_rejected():
    from inseason_support import fixture

    from fba.contracts.base import DataError
    from fba.inseason.matchup import Simulation

    args = list(fixture())
    args[5] = args[5].model_copy(update={"as_of": args[-1] + timedelta(days=1)})
    with pytest.raises(DataError, match="future"):
        Simulation(*args)


@pytest.mark.parametrize(
    "tipoff,offset,hour,fold",
    [
        ("2026-11-01T08:30:00+00:00", -7, 1, 0),
        ("2026-11-01T09:30:00+00:00", -8, 1, 1),
        ("2026-11-01T05:30:00+00:00", -7, 22, 0),
    ],
)
def test_today_display_preserves_instants_across_dst_and_previous_local_day(
    tipoff, offset, hour, fold
):
    from datetime import datetime

    from fba.inseason.today import today

    sim = simulation()
    start = datetime.fromisoformat(tipoff)
    sim.as_of = start - timedelta(hours=1)
    on = start.astimezone(sim.zone).date()
    sim.league = sim.league.model_copy(
        update={
            "matchups": tuple(
                w.model_copy(update={"start": on, "end": on}) if w.id == "2" else w
                for w in sim.league.matchups
            ),
        }
    )
    game = sim.games[0].model_copy(update={"tipoff": start})
    sim.games = (game,)
    sim.players = sim.players.model_copy(update={"games": (game,)})
    result = today(sim, on, "America/Los_Angeles")
    lock = result.locks["p0"]
    # Python deliberately treats ambiguous inter-zone datetimes as unequal;
    # compare their instants, then separately verify the displayed offset/fold.
    assert lock.timestamp() == start.timestamp()
    assert lock.utcoffset() == timedelta(hours=offset)
    assert (lock.hour, lock.fold) == (hour, fold)
    row = next(p for p in result.players if p.player_id == "p0")
    assert row.tipoffs == (lock,)
    assert row.opponents == (game.away,)


def test_streaming_slot_can_release_a_prior_add_but_honors_waiver_clearance():
    from inseason_support import DEFAULTS

    from fba.contracts.inseason import InseasonPreferences
    from fba.inseason.recommendations import candidate_moves

    sim = simulation()
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    first_day = sim.as_of.astimezone(sim.zone).date()
    first = RosterMove(add="p6", drop="p0", effective_on=first_day, starter_games=0)
    roster = ("p6", "p1", "p2")
    tomorrow = first_day + timedelta(days=1)
    options = candidate_moves(sim, prefs, roster, (first,), tomorrow, tomorrow, ())
    assert any(moves[-1].drop == "p6" and moves[-1].add == "p7" for _, moves in options)
    assert all(moves[-1].add != "p0" for _, moves in options)
    cleared = first_day + timedelta(days=sim.league.waiver_days)
    options = candidate_moves(sim, prefs, roster, (first,), cleared, cleared, ())
    assert any(moves[-1].add == "p0" for _, moves in options)
    same_day = candidate_moves(sim, prefs, roster, (first,), first_day, first_day, ())
    assert all(moves[-1].drop != "p6" for _, moves in same_day)


def test_search_deadline_covers_candidate_generation_and_child_simulations(monkeypatch):
    from inseason_support import DEFAULTS

    from fba.contracts.inseason import CalculationTimeout, InseasonPreferences
    from fba.inseason.recommendations import candidate_moves, changed_simulation

    sim = simulation()
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    clock = [0.0]
    monkeypatch.setattr("fba.inseason.matchup.monotonic", lambda: clock[0])
    with pytest.raises(CalculationTimeout, match="recommendations: time budget"):
        with sim.budget("recommendations"):
            child = changed_simulation(sim, "team0", ())
            clock[0] = sim.params.budgets["recommendations"].value + 1
            candidate_moves(
                child,
                prefs,
                child.roster("team0"),
                (),
                sim.as_of.date(),
                sim.league.matchups[-1].end,
                (),
            )
    assert sim.deadline is None
