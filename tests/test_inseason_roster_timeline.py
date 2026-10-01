"""Scheduled moves and IL returns must remain one legal dated roster."""

from datetime import time, timedelta

import pytest
from test_inseason_priority import healthy_il_simulation

from fba.contracts.inseason_results import RosterMove
from fba.inseason.matchup import Simulation
from fba.inseason.recommendations import changed_simulation


def return_case(return_day):
    sim = healthy_il_simulation()
    today = sim.as_of.astimezone(sim.zone).date()
    if return_day:
        sim.players = sim.players.model_copy(
            update={
                "players": tuple(
                    p.model_copy(
                        update={"status": "INJ", "return_on": today + timedelta(days=return_day)}
                    )
                    if p.id == "p6"
                    else p
                    for p in sim.players.players
                )
            }
        )
    return sim, today


@pytest.mark.parametrize("return_day,move_day", ((0, 1), (2, 0), (2, 2), (2, 3)))
def test_il_and_scheduled_move_share_a_legal_timeline(return_day, move_day):
    sim, today = return_case(return_day)
    move_on = today + timedelta(days=move_day)
    move = RosterMove(add="p7", drop="p2", effective_on=move_on, starter_games=0)
    child = changed_simulation(sim, "team0", (move,))
    child.week("team0", "team1", "2")
    returning = child.injury_plan_cache["team0", tuple(sorted(sim.roster("team0")))][0]
    assert returning.player_id == "p6"
    if return_day <= move_day:
        assert returning.drop != "p2"  # This release is reserved for the explicit future move.
    for week in ("2", "3"):
        _, lineups = child.total("team0", week)
        for day in lineups:
            active = (*day.slots.values(), *day.bench)
            assert len(active) == len(set(active)) == 3
            if day.on >= move_on:
                assert "p2" not in active
            if day.on >= returning.effective_on:
                assert "p6" in active
                assert returning.drop not in active


def computed_plan(sim, moves):
    from inseason_support import DEFAULTS

    from fba.contracts.inseason import InseasonPreferences
    from fba.inseason.recommendations import admissible_plan, evaluate_plan
    from fba.inseason.season import season_value

    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    before = sim.week("team0", "team1", "2")
    future = season_value(sim, "team0", after=sim.league.matchups[1].end + timedelta(days=1))
    plan = evaluate_plan(sim, prefs, moves, before, future)
    assert admissible_plan(plan, sim.params.tolerance.value)
    return plan


def test_serialized_combined_f3_plan_is_reused_on_today_without_ros_search(monkeypatch):
    from fba.contracts.inseason_results import AddPlan
    from fba.inseason.today import today

    sim, on = return_case(0)
    move = RosterMove(add="p7", drop="p2", effective_on=on, starter_games=0)
    plan = AddPlan.model_validate_json(computed_plan(sim, (move,)).model_dump_json())
    cold, _ = return_case(0)

    def unexpected(*args, **kwargs):
        raise AssertionError("Today must reuse the saved IL and F3 decisions without ROS")

    monkeypatch.setattr("fba.inseason.injury_returns.return_plan", unexpected)
    monkeypatch.setattr("fba.inseason.season.season_value", unexpected)
    result = today(cold, on, "Asia/Taipei", plan)
    assert result.plan_id == plan.id and not result.injury_pending
    assert result.week_forecast.injury_returns == plan.after.injury_returns
    active = (*result.lineup.slots.values(), *result.lineup.bench)
    assert "p6" in active and "p7" in active and "p2" not in active
    kinds = [a.kind for a in result.actions]
    assert kinds.index("drop") < kinds.index("injury_out") < kinds.index("add_drop")
    assert not any(a.kind == "injury_in" and a.player_id == "p6" for a in result.actions)


@pytest.mark.parametrize("change", ("protected", "source", "missing-return"))
def test_today_rejects_invalid_saved_combined_return_decisions(change, monkeypatch):
    from fba.contracts.base import DataError
    from fba.inseason.today import today

    sim, on = return_case(0)
    move = RosterMove(add="p7", drop="p2", effective_on=on, starter_games=0)
    plan = computed_plan(sim, (move,))
    cold, _ = return_case(0)
    if change == "protected":
        cold.untouchable = frozenset((plan.after.injury_returns[0].drop,))
    elif change == "source":
        cold.players = cold.players.model_copy(
            update={
                "players": tuple(
                    p.model_copy(update={"status": "INJ", "return_on": None}) if p.id == "p6" else p
                    for p in cold.players.players
                )
            }
        )
    else:
        plan = plan.model_copy(
            update={"after": plan.after.model_copy(update={"injury_returns": ()})}
        )

    def unexpected(*args, **kwargs):
        raise AssertionError("An invalid saved return must not start a new ROS search")

    monkeypatch.setattr("fba.inseason.injury_returns.return_plan", unexpected)
    with pytest.raises(DataError, match="IL|回歸|計畫"):
        today(cold, on, "Asia/Taipei", plan)


class ExplicitTimelineSimulation(Simulation):
    """Independent known-roster oracle; no product return or timeline helper."""

    def __init__(self, source, events):
        super().__init__(
            source.league,
            source.params,
            source.players,
            source.priors,
            source.ledger,
            source.snapshot,
            source.as_of,
            source.params.season_simulations.value,
        )
        self.events = sorted(events)
        self.project_injury_returns = False

    def projected_roster(self, team, on, roster):
        if team == "team0":
            for effective, _, add, drop in self.events:
                if effective <= on:
                    if add in roster or (drop is not None and drop not in roster):
                        raise ValueError("Independent oracle: illegal roster event")
                    roster = tuple(p for p in roster if p != drop) + (add,)
        assert len(roster) == len(set(roster)) <= 3
        return tuple(sorted(roster))


@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
@pytest.mark.parametrize("return_day,move_day", ((0, 1), (2, 0), (2, 2), (2, 3)))
def test_release_choice_matches_all_legal_drops_with_independent_roster_oracle(
    mode, return_day, move_day
):
    from fba.inseason.season import season_value

    sim, today = return_case(return_day)
    sim.league = sim.league.model_copy(update={"scoring": mode})
    return_on, move_on = today + timedelta(days=return_day), today + timedelta(days=move_day)
    values = {}
    for drop in ("p0", "p1", "p2", "p7"):
        events = ((return_on, 0, "p6", drop), (move_on, 1, "p7", "p2"))
        oracle = ExplicitTimelineSimulation(sim, events)
        try:
            oracle.projected_roster("team0", sim.league.ends_on, sim.roster("team0"))
        except ValueError:
            continue
        values[drop] = season_value(oracle, "team0", after=return_on, include_playoffs=True)
    assert values
    expected = next(iter(values))
    for drop, value in values.items():
        if value > values[expected] + sim.params.tolerance.value:
            expected = drop
    move = RosterMove(add="p7", drop="p2", effective_on=move_on, starter_games=0)
    child = changed_simulation(sim, "team0", (move,))
    child.projected_roster("team0", sim.league.ends_on, sim.roster("team0"))
    actual = child.injury_plan_cache["team0", sim.roster("team0")][0]
    assert actual.drop == expected


def test_later_return_preserves_a_scheduled_release_of_the_first_returning_player():
    sim, today = return_case(0)
    sim.league = sim.league.model_copy(
        update={
            "injury_slots": tuple(
                s.model_copy(update={"count": 2}) for s in sim.league.injury_slots
            )
        }
    )
    sim.snapshot = sim.snapshot.model_copy(
        update={
            "teams": tuple(
                t.model_copy(update={"injury_players": {"p6": "IL", "p8": "IL"}})
                if t.id == "team0"
                else t
                for t in sim.snapshot.teams
            ),
            "free_agents": tuple(f for f in sim.snapshot.free_agents if f.player_id != "p8"),
        }
    )
    sim.players = sim.players.model_copy(
        update={
            "players": tuple(
                p.model_copy(update={"status": "INJ", "return_on": today + timedelta(days=2)})
                if p.id == "p8"
                else p
                for p in sim.players.players
            )
        }
    )
    move = RosterMove(add="p7", drop="p6", effective_on=today + timedelta(days=3), starter_games=0)
    child = changed_simulation(sim, "team0", (move,))
    active = child.projected_roster("team0", sim.league.ends_on, sim.roster("team0"))
    returning = child.injury_plan_cache["team0", sim.roster("team0")]
    assert [r.player_id for r in returning] == ["p6", "p8"]
    assert returning[1].drop != "p6"
    assert "p8" in active and "p7" in active and "p6" not in active


def test_explicit_release_before_the_player_returns_fails_clearly():
    from fba.contracts.base import DataError

    sim, today = return_case(2)
    move = RosterMove(add="p7", drop="p6", effective_on=today, starter_games=0)
    child = changed_simulation(sim, "team0", (move,))
    with pytest.raises(DataError, match="cannot add p7 / drop p6"):
        child.week("team0", "team1", "2")


def test_il_with_chained_future_moves_preserves_each_event_once():
    sim, on = return_case(0)
    moves = (
        RosterMove(add="p7", drop="p2", effective_on=on + timedelta(days=1), starter_games=0),
        RosterMove(add="p8", drop="p7", effective_on=on + timedelta(days=2), starter_games=0),
    )
    child = changed_simulation(sim, "team0", moves)
    active = child.projected_roster("team0", sim.league.ends_on, sim.roster("team0"))
    assert len(active) == 3 and "p6" in active and "p8" in active
    assert "p2" not in active and "p7" not in active
    assert child.projected_roster("team0", sim.league.ends_on, sim.roster("team0")) == active
    assert sim.roster("team0") == ("p0", "p1", "p2")


def test_a_player_can_be_reacquired_after_the_il_release_before_a_later_explicit_drop():
    sim, on = return_case(0)
    moves = (
        RosterMove(add="p2", drop="p0", effective_on=on + timedelta(days=1), starter_games=0),
        RosterMove(add="p7", drop="p2", effective_on=on + timedelta(days=2), starter_games=0),
    )
    child = changed_simulation(sim, "team0", moves)
    active = child.projected_roster("team0", sim.league.ends_on, sim.roster("team0"))
    assert child.injury_plan_cache["team0", sim.roster("team0")][0].drop == "p2"
    assert set(active) == {"p1", "p6", "p7"}


def test_today_gives_a_reacquired_il_release_a_final_lineup_action_and_single_detail():
    from fba.inseason.today import today

    sim, on = return_case(0)
    next_day = on + timedelta(days=1)
    moves = (
        RosterMove(add="p2", drop="p0", effective_on=next_day, starter_games=0),
        RosterMove(add="p7", drop="p1", effective_on=next_day, starter_games=0),
    )
    plan = computed_plan(sim, moves)
    cold, _ = return_case(0)
    result = today(cold, next_day, "Asia/Taipei", plan)
    active = (*result.lineup.slots.values(), *result.lineup.bench)
    assert "p2" in active and plan.after.injury_returns[0].drop == "p2"
    actions = [a for a in result.actions if a.player_id == "p2"]
    assert sum(a.kind in ("start", "bench") for a in actions) == 1
    assert sum(p.player_id == "p2" for p in result.players) == 1


def test_same_day_chained_moves_appear_in_their_legal_execution_order():
    from fba.inseason.today import today

    sim, on = return_case(0)
    next_day = on + timedelta(days=1)
    moves = (
        RosterMove(add="p7", drop="p2", effective_on=next_day, starter_games=0),
        RosterMove(add="p8", drop="p7", effective_on=next_day, starter_games=0),
    )
    plan = computed_plan(sim, moves)
    cold, _ = return_case(0)
    result = today(cold, next_day, "Asia/Taipei", plan)
    assert [a.player_id for a in result.actions if a.kind == "add_drop"] == ["p7", "p8"]
    assert "p8" in (*result.lineup.slots.values(), *result.lineup.bench)
    assert "p7" not in (*result.lineup.slots.values(), *result.lineup.bench)


def test_a_future_acquisition_of_an_already_owned_player_fails_clearly():
    from fba.contracts.base import DataError

    sim, on = return_case(0)
    moves = (
        RosterMove(add="p7", drop="p2", effective_on=on + timedelta(days=1), starter_games=0),
        RosterMove(add="p7", drop="p1", effective_on=on + timedelta(days=2), starter_games=0),
    )
    child = changed_simulation(sim, "team0", moves)
    with pytest.raises(DataError, match="cannot add p7 / drop p1"):
        child.week("team0", "team1", "2")


def test_same_day_release_of_a_returning_player_keeps_today_actions_in_legal_order():
    from fba.contracts.inseason import MatchupPriority
    from fba.inseason.today import today

    sim, on = return_case(0)
    sim.priority_cache["2"] = MatchupPriority(status="must_win", reason="synthetic knockout")
    move = RosterMove(add="p7", drop="p6", effective_on=on, starter_games=0)
    plan = computed_plan(sim, (move,))
    cold, _ = return_case(0)
    cold.priority_cache = sim.priority_cache.copy()
    result = today(cold, on, "Asia/Taipei", plan)
    active = (*result.lineup.slots.values(), *result.lineup.bench)
    assert "p7" in active and "p6" not in active
    kinds = [a.kind for a in result.actions]
    assert kinds.index("drop") < kinds.index("injury_out") < kinds.index("add_drop")
    assert not any(
        a.kind in ("start", "bench", "injury_in") and a.player_id == "p6" for a in result.actions
    )


@pytest.mark.parametrize("capacity", (4, 5))
def test_il_return_uses_free_capacity_without_releasing_anyone(capacity):
    sim, on = return_case(0)
    sim.league = sim.league.model_copy(update={"bench_slots": capacity - 2})
    move = RosterMove(add="p7", drop="p2", effective_on=on + timedelta(days=1), starter_games=0)
    child = changed_simulation(sim, "team0", (move,))
    active = child.projected_roster("team0", sim.league.ends_on, sim.roster("team0"))
    assert set(active) == {"p0", "p1", "p6", "p7"}
    assert child.injury_plan_cache["team0", sim.roster("team0")][0].drop is None


def test_cancelled_combined_il_plan_does_not_publish_partial_decisions():
    from fba.inseason.matchup import CalculationTimeout

    sim, on = return_case(0)
    move = RosterMove(add="p7", drop="p2", effective_on=on, starter_games=0)
    child = changed_simulation(sim, "team0", (move,))
    child.cancelled = lambda: True
    with pytest.raises(CalculationTimeout, match="cancelled"):
        child.week("team0", "team1", "2")
    assert not child.injury_plan_cache and not child.forecast_cache


def test_fully_locked_returns_wait_for_a_legal_release_date():
    sim, on = return_case(0)
    sim.league = sim.league.model_copy(update={"lineup_lock": "daily", "lineup_lock_time": time(0)})
    move = RosterMove(add="p7", drop="p2", effective_on=on + timedelta(days=2), starter_games=0)
    child = changed_simulation(sim, "team0", (move,))
    child.projected_roster("team0", sim.league.ends_on, sim.roster("team0"))
    returning = child.injury_plan_cache["team0", sim.roster("team0")][0]
    assert returning.effective_on == on + timedelta(days=1)
    assert not child.locked(returning.drop, returning.effective_on)
