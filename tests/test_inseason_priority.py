"""Finite-outcome oracles and policy regressions, independent of forecasts."""

from itertools import product

import numpy as np
import pytest
from inseason_support import DEFAULTS, simulation

from fba.contracts.inseason import InseasonPreferences, MatchupPriority
from fba.contracts.inseason_results import AddPlan, RosterMove
from fba.core.qualification import qualification_possible
from fba.inseason.priority import matchup_priority
from fba.inseason.recommendations import admissible_plan, candidate_moves
from fba.inseason.today import today


@pytest.mark.parametrize("case", range(30))
def test_qualification_matches_all_discrete_remaining_results(case):
    rng = np.random.default_rng(9100 + case)
    points = tuple(float(x) / 2 for x in rng.integers(0, 8, size=4))
    order = tuple(int(x) for x in rng.permutation(4))
    ties = (0.5,) if case < 20 else (0.0, 0.5, 1.0)
    fixtures = ((0, 1), (2, 3)) if len(ties) > 1 else ((0, 1), (2, 3), (0, 2), (1, 3))
    for win in (False, True):
        possible = False
        for outcomes in product((0, 1, 2), repeat=len(fixtures) * len(ties)):
            first = outcomes[: len(ties)]
            difference = first.count(0) - first.count(1)
            if (difference > 0 if win else difference < 0) is False:
                continue
            totals = list(points)
            for i, (a, b) in enumerate(fixtures):
                for j, tie in enumerate(ties):
                    outcome = outcomes[i * len(ties) + j]
                    totals[a] += 1 if outcome == 0 else tie if outcome == 2 else 0
                    totals[b] += 1 if outcome == 1 else tie if outcome == 2 else 0
            ranks = sorted(range(4), key=lambda i: (-totals[i], order[i]))
            if ranks.index(0) < 2:
                possible = True
                break
        assert (
            qualification_possible(points, order, 0, 2, fixtures, 0, ties, win, 5.0, 1e-9)
            is possible
        )


@pytest.mark.parametrize(
    "points,status",
    [((1.5, 2, 0, 0), "must_win"), ((2, 1, 0, 0), "normal"), ((0, 4, 3, 2), "eliminated")],
)
def test_priority_is_based_on_remaining_schedule_feasibility(points, status):
    sim = simulation(teams=4)
    sim.league = sim.league.model_copy(
        update={
            "matchups": tuple(w for w in sim.league.matchups if w.id == "2"),
            "playoff_seeding": "overall",
        }
    )
    sim.snapshot = sim.snapshot.model_copy(
        update={
            "pairings": tuple(p for p in sim.snapshot.pairings if p.week_id == "2"),
            "teams": tuple(
                t.model_copy(
                    update={"wins": float(points[i]), "losses": 4 - points[i], "ties": 0.0}
                )
                for i, t in enumerate(sim.snapshot.teams)
            ),
        }
    )
    result = matchup_priority(sim, "2")
    assert result.status == status
    if status == "must_win":
        assert result.qualification_if_loss is False and result.qualification_if_win is True


def test_unknown_schedule_or_playoff_bracket_does_not_enable_sacrifice():
    sim = simulation(teams=4)
    sim.snapshot = sim.snapshot.model_copy(update={"pairings": sim.snapshot.pairings[:-1]})
    assert matchup_priority(sim, "4").status == "unknown"
    sim.priority_cache.clear()
    sim.snapshot = sim.snapshot.model_copy(
        update={
            "pairings": tuple(
                p.model_copy(update={"elimination": True}) if p.week_id == "4" else p
                for p in sim.snapshot.pairings
            ),
        }
    )
    assert matchup_priority(sim, "4").status == "must_win"


def test_unknown_seeding_and_standings_tie_do_not_enable_sacrifice():
    sim = simulation(teams=4)
    assert matchup_priority(sim, "2").status == "unknown"
    sim.league = sim.league.model_copy(
        update={
            "playoff_seeding": "overall",
            "matchups": tuple(w for w in sim.league.matchups if w.id == "2"),
        }
    )
    sim.snapshot = sim.snapshot.model_copy(
        update={
            "pairings": tuple(p for p in sim.snapshot.pairings if p.week_id == "2"),
            "teams": tuple(
                t.model_copy(
                    update={
                        "wins": float((1, 2, 0, 0)[i]),
                        "losses": float((1, 0, 2, 2)[i]),
                        "ties": 0.0,
                    }
                )
                for i, t in enumerate(sim.snapshot.teams)
            ),
        }
    )
    sim.priority_cache.clear()
    # Winning only reaches a standings tie. No false claim that a current seed
    # guarantees qualification or justifies sacrificing long-term strength.
    assert matchup_priority(sim, "2").status == "unknown"


def test_a_normal_plan_cannot_hide_playoff_loss_behind_this_weeks_gain(monkeypatch):
    from fba.inseason import recommendations

    sim = simulation()
    before = sim.week("team0", "team1", "2")
    child = recommendations.changed_simulation(sim, "team0", ())
    monkeypatch.setattr(
        child, "week", lambda *args: before.model_copy(update={"score": before.score + 0.2})
    )
    monkeypatch.setattr(recommendations, "changed_simulation", lambda *args: child)
    calls = []

    def value(engine, team, rosters=None, *, after, include_playoffs=False):
        calls.append(include_playoffs)
        return -0.25 if engine is child and include_playoffs else 0.0

    monkeypatch.setattr(recommendations, "season_value", value)
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    plan = recommendations.evaluate_plan(sim, prefs, (), before, 0.0)
    assert plan.delta_week == pytest.approx(0.2) and plan.delta_season == 0.0
    assert plan.delta_strength == -0.25 and plan.score > 0
    assert any(calls) and not admissible_plan(plan, sim.params.tolerance.value)


def test_a_certified_emergency_plan_does_not_start_a_season_search(monkeypatch):
    from fba.inseason import recommendations

    sim = simulation()
    before = sim.week("team0", "team1", "2").model_copy(
        update={"priority": MatchupPriority(status="must_win", reason="confirmed knockout")}
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("emergency plan must only evaluate this week")

    monkeypatch.setattr(recommendations, "season_value", unexpected)
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    plan = recommendations.evaluate_plan(sim, prefs, (), before, 0.0)
    assert plan.delta_season is None and plan.delta_strength is None
    assert plan.score == plan.delta_week


@pytest.mark.parametrize("status", ["normal", "unknown", "eliminated", "must_win"])
def test_only_certified_must_win_can_accept_a_long_term_loss(status):
    sim = simulation()
    before = sim.week("team0", "team1", "2")
    plan = AddPlan(
        id="plan",
        moves=(),
        before=before,
        after=before,
        delta_week=0.4,
        delta_season=-0.1,
        delta_strength=-0.1,
        score=0.3,
        traces=(),
        priority=MatchupPriority(status=status, reason="test"),
    )
    assert admissible_plan(plan, 1e-9) is (status == "must_win")
    assert not admissible_plan(plan.model_copy(update={"delta_week": 0.0}), 1e-9)
    if status != "must_win":
        assert not admissible_plan(plan.model_copy(update={"delta_strength": None}), 1e-9)


def test_today_reuses_the_plan_without_running_any_season_search(monkeypatch):
    from fba.inseason import season

    sim = simulation()
    on = sim.as_of.astimezone(sim.zone).date()
    before = sim.week("team0", "team1", "2")
    plan = AddPlan(
        id="saved",
        moves=(RosterMove(add="p6", drop="p0", effective_on=on, starter_games=0),),
        before=before,
        after=before,
        delta_week=0.2,
        delta_season=0.1,
        delta_strength=0.1,
        score=0.3,
        traces=(),
        priority=before.priority,
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("today must not start a season search")

    monkeypatch.setattr(season, "season_value", unexpected)
    monkeypatch.setattr(season, "season_forecasts", unexpected)
    result = today(sim, on, "Asia/Taipei", plan)
    assert result.recommendation is plan
    assert result.week_forecast.no_moves_score == before.score
    assert "p0" not in result.lineup.slots.values()
    assert not any(a.kind == "start" and a.player_id == "p0" for a in result.actions)
    assert any(a.player_id == "p6" and a.kind in ("start", "bench") for a in result.actions)
    assert any(a.kind == "add_drop" and a.player_id == "p6" for a in result.actions)
    assert today(sim, on, "Asia/Taipei").recommendation is None


def test_screening_prioritizes_only_low_contribution_players_and_keeps_full_oracle():
    sim = simulation()
    sim.params = sim.params.model_copy(
        update={"drop_shortlist": sim.params.drop_shortlist.model_copy(update={"value": 1})}
    )
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    on = sim.as_of.astimezone(sim.zone).date()
    screened = candidate_moves(sim, prefs, sim.roster("team0"), (), on, on, ())
    complete = candidate_moves(sim, prefs, sim.roster("team0"), (), on, on, (), exhaustive=True)
    assert len({moves[-1].drop for _, moves in screened}) == 1
    assert len({moves[-1].drop for _, moves in complete}) == 3
    assert len(screened) < len(complete)


def test_latest_saved_plan_reuses_valid_context_and_rejects_updated_inputs(tmp_path, monkeypatch):
    from test_inseason_review import prediction
    from test_inseason_service import selected_session

    from fba.inseason.operations import latest_plan, recorded_plan
    from fba.inseason.session import json_value

    sim = simulation()
    session = selected_session(tmp_path)
    session.params = sim.params
    state = session.state().model_copy(update={"normalized_sha256": "a" * 64})
    session.save_state(state)
    record = prediction(sim)
    plan = AddPlan(
        id="saved-valid",
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
    session.league_store().append_snapshot(
        "predictions", "test", sim.as_of, json_value(record.model_dump(mode="json"))
    )
    assert latest_plan(session, "2") == plan
    assert latest_plan(session, "3") is None
    # An explicit old choice reports stale inputs. The automatic lookup leaves
    # the plan absent; neither runs a new recommendations/ROS calculation.
    session.save_state(state.model_copy(update={"normalized_sha256": "b" * 64}))
    assert latest_plan(session, "2") is None
    from fba.contracts.base import DataError

    with pytest.raises(DataError, match="重新計算 F3"):
        recorded_plan(session, plan.id)

    def expired():
        raise TimeoutError("history lookup exceeded Today budget")

    with pytest.raises(TimeoutError, match="Today budget"):
        latest_plan(session, "2", expired)
    with pytest.raises(TimeoutError, match="Today budget"):
        recorded_plan(session, plan.id, expired)


def test_cold_today_with_full_roster_and_healthy_il_never_searches_the_season(monkeypatch):
    from fba.contracts.config import InjurySlot
    from fba.inseason import season

    sim = simulation()
    sim.league = sim.league.model_copy(
        update={
            "injury_slots": (InjurySlot(id="IL", label="IL", count=1, eligible_statuses=("INJ",)),)
        }
    )
    sim.snapshot = sim.snapshot.model_copy(
        update={
            "teams": tuple(
                t.model_copy(update={"injury_players": {"p6": "IL"}}) if t.id == "team0" else t
                for t in sim.snapshot.teams
            ),
            "free_agents": tuple(f for f in sim.snapshot.free_agents if f.player_id != "p6"),
        }
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("cold Today must not search IL drop/ROS candidates")

    monkeypatch.setattr(season, "season_value", unexpected)
    monkeypatch.setattr(season, "season_forecasts", unexpected)
    result = today(sim, sim.as_of.astimezone(sim.zone).date(), "Asia/Taipei")
    assert result.injury_pending == ("p6",)
    assert not any(a.kind == "drop" for a in result.actions)
    assert not any(a.player_id == "p6" and a.kind == "start" for a in result.actions)
    assert "名單已滿" in next(a.reason for a in result.actions if a.kind == "injury_out")
    assert len(result.lineup.slots) + len(result.lineup.bench) <= 3


def test_today_api_invalidates_saved_plan_when_reserve_adds_changes(tmp_path, monkeypatch):
    from test_inseason_review import prediction
    from test_inseason_service import selected_session

    from fba.apps.inseason.api import today_result
    from fba.contracts.base import DataError
    from fba.contracts.inseason_app import TodayRequest
    from fba.inseason.forecast_records import recommendation_policy
    from fba.inseason.recommendations import evaluate_plan
    from fba.inseason.session import json_value

    sim = simulation()
    session = selected_session(tmp_path)
    session.params = sim.params
    session.save_state(session.state().model_copy(update={"normalized_sha256": "a" * 64}))
    on = sim.as_of.astimezone(sim.zone).date()
    record = prediction(sim)
    move = RosterMove(add="p6", drop="p0", effective_on=on, starter_games=0)
    from datetime import timedelta

    from fba.inseason.season import season_value

    future = season_value(
        sim,
        "team0",
        after=next(w.end for w in sim.league.matchups if w.id == "2") + timedelta(days=1),
    )
    plan = evaluate_plan(sim, session.preferences, (move,), record.with_adjustments, future)
    assert admissible_plan(plan, sim.params.tolerance.value)
    record = record.model_copy(
        update={
            "recommendations": (plan,),
            "recommendation_policy_sha256": recommendation_policy(session.preferences),
        }
    )
    session.league_store().append_snapshot(
        "predictions", "test", sim.as_of, json_value(record.model_dump(mode="json"))
    )
    monkeypatch.setattr(session, "simulation", lambda: sim)
    initial = today_result(session, TodayRequest(on=on, plan_id=None))
    assert initial["plan_id"] == plan.id
    assert "p0" not in initial["lineup"]["slots"].values()
    assert any(
        a["player_id"] == "p6" and a["kind"] in ("start", "bench") for a in initial["actions"]
    )
    session.preferences = session.preferences.model_copy(
        update={"reserve_adds": sim.league.adds_per_week}
    )
    result = today_result(session, TodayRequest(on=on, plan_id=None))
    assert result["recommendation"] is None
    assert not any(a["kind"] == "add_drop" for a in result["actions"])
    with pytest.raises(DataError, match="重新計算 F3"):
        today_result(session, TodayRequest(on=on, plan_id=plan.id))


def test_today_api_setup_latency_counts_against_the_complete_deadline(tmp_path, monkeypatch):
    from time import sleep

    from test_inseason_service import selected_session

    from fba.apps.inseason.api import today_result
    from fba.contracts.inseason import CalculationTimeout
    from fba.contracts.inseason_app import TodayRequest

    sim = simulation()
    sim.params = sim.params.model_copy(
        update={
            "budgets": {
                **sim.params.budgets,
                "today": sim.params.budgets["today"].model_copy(update={"value": 0.02}),
            }
        }
    )
    session = selected_session(tmp_path)

    def delayed_setup():
        sleep(0.04)
        return sim

    monkeypatch.setattr(session, "simulation", delayed_setup)
    with pytest.raises(CalculationTimeout, match="time budget exceeded"):
        today_result(session, TodayRequest(on=sim.as_of.astimezone(sim.zone).date(), plan_id=None))
    assert not session.league_store().history("daily-predictions")


@pytest.mark.parametrize("missed", ("yesterday", "cutoff", "locked"))
def test_today_rejects_unexecuted_moves_after_their_legal_effective_time(missed):
    from datetime import timedelta

    from fba.contracts.base import DataError

    sim = simulation()
    on = sim.as_of.astimezone(sim.zone).date()
    forecast = sim.week("team0", "team1", "2")
    effective_on = on - timedelta(days=1) if missed == "yesterday" else on
    plan = AddPlan(
        id="unexecuted",
        moves=(RosterMove(add="p6", drop="p0", effective_on=effective_on, starter_games=0),),
        before=forecast,
        after=forecast,
        delta_week=0.2,
        delta_season=0.1,
        delta_strength=0.1,
        score=0.3,
        traces=(),
    )
    if missed == "cutoff":
        sim.league = sim.league.model_copy(
            update={"cutoff_local_time": sim.as_of.astimezone(sim.zone).time().replace(tzinfo=None)}
        )
    elif missed == "locked":
        sim.as_of = next(g.tipoff for g in sim.games if g.home == "NBA0")
    with pytest.raises(DataError, match="超過生效或球員鎖定時間"):
        today(sim, on, "Asia/Taipei", plan)
    assert sim.roster("team0") == ("p0", "p1", "p2")
    assert not sim.transitions


def healthy_il_simulation():
    from fba.contracts.config import InjurySlot

    sim = simulation()
    sim.league = sim.league.model_copy(
        update={
            "injury_slots": (InjurySlot(id="IL", label="IL", count=1, eligible_statuses=("INJ",)),)
        }
    )
    sim.snapshot = sim.snapshot.model_copy(
        update={
            "teams": tuple(
                t.model_copy(update={"injury_players": {"p6": "IL"}}) if t.id == "team0" else t
                for t in sim.snapshot.teams
            ),
            "free_agents": tuple(f for f in sim.snapshot.free_agents if f.player_id != "p6"),
        }
    )
    return sim


def test_il_returns_filter_protected_players_and_today_rechecks_cached_decisions(monkeypatch):
    from fba.inseason import season

    sim = healthy_il_simulation()
    sim.untouchable = frozenset(("p1", "p2"))
    forecast = sim.week("team0", "team1", "2")
    assert tuple(m.drop for m in forecast.injury_returns) == ("p0",)
    sim.untouchable = frozenset(sim.roster("team0"))

    def unexpected(*args, **kwargs):
        raise AssertionError("Today must not search again after protection invalidates an IL plan")

    monkeypatch.setattr(season, "season_value", unexpected)
    monkeypatch.setattr(season, "season_forecasts", unexpected)
    result = today(sim, sim.as_of.astimezone(sim.zone).date(), "Asia/Taipei")
    assert result.injury_pending == ("p6",)
    assert not any(a.kind == "drop" for a in result.actions)
    assert "p6" not in result.lineup.slots.values()
    assert "名單已滿" in next(a.reason for a in result.actions if a.kind == "injury_out")


def test_all_protected_players_leave_il_activation_explicitly_pending():
    from fba.contracts.base import DataError

    sim = healthy_il_simulation()
    sim.untouchable = frozenset(sim.roster("team0"))
    with pytest.raises(DataError, match="沒有可釋出的未保護球員"):
        sim.week("team0", "team1", "2")
    result = today(sim, sim.as_of.astimezone(sim.zone).date(), "Asia/Taipei")
    assert result.injury_pending == ("p6",)
    assert not any(a.kind == "drop" for a in result.actions)
    assert set(result.lineup.slots.values()) <= sim.untouchable


def test_today_api_protection_changes_invalidate_cached_il_forecasts(tmp_path, monkeypatch):
    from test_inseason_acceptance_gaps import prepared_session

    from fba.apps.inseason.api import today_result
    from fba.contracts.inseason_app import TodayRequest
    from fba.inseason.session import json_value

    sim = healthy_il_simulation()
    session, now = prepared_session(tmp_path)
    session.params = sim.params
    sha = session.league_store().snapshot(
        "test IL", now, json_value(sim.snapshot.model_dump(mode="json"))
    )
    session.save_state(
        session.state().model_copy(update={"league": sim.league, "normalized_sha256": sha})
    )
    factory = session.simulation
    first = factory(as_of=now, require_league=False)
    assert first.week("team0", "team1", "2").injury_returns
    assert first.injury_plan_cache and first.forecast_cache
    session.preferences = session.preferences.model_copy(
        update={"untouchable": sim.roster("team0")}
    )
    changed = factory(as_of=now, require_league=False)
    assert changed.untouchable == frozenset(sim.roster("team0"))
    assert changed.season().untouchable == changed.untouchable
    assert not changed.injury_plan_cache and not changed.forecast_cache
    monkeypatch.setattr(session, "simulation", lambda: factory(as_of=now, require_league=False))
    result = today_result(session, TodayRequest(on=now.astimezone(sim.zone).date(), plan_id=None))
    assert result["injury_pending"] == ["p6"]
    assert not any(a["kind"] == "drop" for a in result["actions"])


def test_two_moves_can_win_when_every_single_move_has_zero_gain():
    from fba.inseason.recommendations import evaluate_plan, search_adds
    from fba.inseason.replay_oracle import exhaustive_plans

    sim = simulation()
    sim.league = sim.league.model_copy(update={"adds_per_week": 2})
    sim.priority_cache["2"] = MatchupPriority(status="must_win", reason="synthetic knockout")
    for game in sim.games:
        pid = next(p.id for p in sim.players.players if p.team_id == game.home)
        i = int(pid[1:])
        value = 2.0 if i < 3 else 3.0 if i < 6 else 4.0
        draw = np.full((sim.samples, len(sim.axes)), value)
        draw[:, sim.axes.index("TO")] = 0
        draw[:, sim.axes.index("PTS")] = 4 * value
        sim.draws[pid, game.id] = draw
    prefs = InseasonPreferences.model_validate_json(
        (DEFAULTS / "preferences.json").read_bytes()
    ).model_copy(update={"reserve_adds": 0})
    on = sim.as_of.astimezone(sim.zone).date()
    before = sim.week("team0", "team1", "2")
    moves = (
        RosterMove(add="p6", drop="p0", effective_on=on, starter_games=0),
        RosterMove(add="p7", drop="p1", effective_on=on, starter_games=0),
    )
    one = evaluate_plan(sim, prefs, moves[:1], before, 0.0)
    both = evaluate_plan(sim, prefs, moves, before, 0.0)
    assert one.delta_week == 0.0 and not admissible_plan(one, sim.params.tolerance.value)
    assert both.delta_week == pytest.approx(0.8)
    assert admissible_plan(both, sim.params.tolerance.value)
    count, retained_zero = exhaustive_plans(sim, prefs, before, 0.0, 2, 1000, 0.0)
    assert count > 18 and retained_zero is False
    assert exhaustive_plans(sim, prefs, before, 0.0, 2, 1000, both.score)[1] is True
    recommended = search_adds(sim, prefs, "2", lambda _: None)
    assert recommended and recommended[0].delta_week == pytest.approx(0.8)
    assert all(admissible_plan(p, sim.params.tolerance.value) for p in recommended)


def test_il_drop_compares_remaining_playoffs_and_keeps_unknown_opponents_explicit():
    from fba.inseason.injury_returns import return_plan
    from fba.inseason.matchup import Simulation
    from fba.inseason.season import MissingSeasonOpponent, season_value

    sim = healthy_il_simulation()
    sim.league = sim.league.model_copy(
        update={
            "matchups": tuple(
                w.model_copy(update={"phase": "playoff"}) for w in sim.league.matchups
            ),
            "playoff_weeks": tuple(w.id for w in sim.league.matchups),
        }
    )
    on = sim.as_of.astimezone(sim.zone).date()
    values = {}
    for drop in sim.roster("team0"):
        child = Simulation(
            sim.league,
            sim.params,
            sim.players,
            sim.priors,
            sim.ledger,
            sim.snapshot,
            sim.as_of,
            sim.params.season_simulations.value,
        )
        child.project_injury_returns = False
        child.transitions["team0"] = (
            (on, tuple(p for p in sim.roster("team0") if p != drop) + ("p6",)),
        )
        assert season_value(child, "team0", after=on) == 0.0
        values[drop] = season_value(child, "team0", after=on, include_playoffs=True)
    assert values["p1"] > values["p0"]
    plan = return_plan(sim, "team0", sim.roster("team0"))
    assert plan[0].drop == max(values, key=lambda p: values[p]) == "p1"
    sim.snapshot = sim.snapshot.model_copy(
        update={"pairings": tuple(p for p in sim.snapshot.pairings if p.week_id != "3")}
    )
    with pytest.raises(MissingSeasonOpponent, match="actual remaining opponent is missing"):
        return_plan(sim, "team0", sim.roster("team0"))
