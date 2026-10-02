"""Independent release-path oracles for simultaneous and staggered IL returns."""

from datetime import timedelta
from time import monotonic

import numpy as np
import pytest
from inseason_support import fixture
from test_inseason_roster_timeline import ExplicitTimelineSimulation

from fba.contracts.config import InjurySlot, Matchup
from fba.contracts.inseason import CalculationTimeout, SeasonGame
from fba.contracts.inseason_results import RosterMove
from fba.inseason.injury_returns import return_plan
from fba.inseason.matchup import Simulation
from fba.inseason.season import season_value


def simulation_with_two_returns(seed, return_day, mode):
    args = list(fixture(mode))
    now = args[-1]
    on = now.date()
    args[0] = args[0].model_copy(
        update={
            "injury_slots": (InjurySlot(id="IL", label="IL", count=2, eligible_statuses=("INJ",)),),
            "matchups": (Matchup(id="2", start=on, end=on + timedelta(days=3), phase="regular"),),
            "ends_on": on + timedelta(days=3),
            "playoff_weeks": (),
        }
    )
    games = tuple(
        SeasonGame(
            id=f"il:{pid}:{day}",
            home=f"NBA{pid}",
            away="VISITOR",
            tipoff=now + timedelta(days=day, hours=12),
            known_at=now - timedelta(days=1),
            status="scheduled",
        )
        for pid in range(9)
        for day in range(4)
    )
    args[2] = args[2].model_copy(
        update={
            "games": games,
            "players": tuple(
                p.model_copy(
                    update={
                        "status": "INJ" if return_day else "healthy",
                        "return_on": on + timedelta(days=return_day),
                    }
                )
                if p.id == "p7"
                else p
                for p in args[2].players
            ),
        }
    )
    args[5] = args[5].model_copy(
        update={
            "teams": tuple(
                t.model_copy(update={"injury_players": {"p6": "IL", "p7": "IL"}})
                if t.id == "team0"
                else t
                for t in args[5].teams
            ),
            "pairings": tuple(p for p in args[5].pairings if p.week_id == "2"),
            "free_agents": tuple(f for f in args[5].free_agents if f.player_id not in ("p6", "p7")),
        }
    )
    sim = Simulation(*args, samples=args[1].season_simulations.value)
    rng = np.random.default_rng(seed)
    for game in games:
        box = rng.poisson(4, (sim.samples, len(sim.axes))).astype(float)
        for made, attempted in (("FGM", "FGA"), ("FTM", "FTA"), ("3PM", "FGM")):
            box[:, sim.axes.index(made)] = np.minimum(
                box[:, sim.axes.index(made)], box[:, sim.axes.index(attempted)]
            )
        box[:, sim.axes.index("PTS")] = (
            2 * box[:, sim.axes.index("FGM")]
            + box[:, sim.axes.index("FTM")]
            + box[:, sim.axes.index("3PM")]
        )
        sim.draws[f"p{game.home[3:]}", game.id] = box
    return sim


def independent_release_score(sim, drops, return_day):
    on = sim.as_of.astimezone(sim.zone).date()
    events = ((on, 0, "p6", drops[0]), (on + timedelta(days=return_day), 1, "p7", drops[1]))
    oracle = ExplicitTimelineSimulation(sim, events)
    oracle.draws = sim.draws
    # The independent oracle applies known legal roster events. It does not call
    # return_plan, feasible_returns, return_scenario or roster_after.
    return season_value(oracle, "team0", include_playoffs=True)


@pytest.mark.parametrize("seed", range(18000, 18030))
@pytest.mark.parametrize("return_day", (0, 2))
@pytest.mark.parametrize("mode", ("h2h_one_win", "h2h_each_category"))
def test_multiple_il_returns_match_all_legal_release_paths(seed, return_day, mode):
    sim = simulation_with_two_returns(seed, return_day, mode)
    plan = return_plan(sim, "team0", ("p0", "p1", "p2"))
    drops = tuple(p.drop for p in plan)
    assert tuple(p.player_id for p in plan) == ("p6", "p7")
    paths = [
        (first, second)
        for first in ("p0", "p1", "p2")
        for second in (*tuple(p for p in ("p0", "p1", "p2") if p != first), "p6")
    ]
    assert len(set(paths)) == 9
    best = max(independent_release_score(sim, path, return_day) for path in paths)
    got = independent_release_score(sim, drops, return_day)
    assert got == pytest.approx(best, abs=sim.params.tolerance.value, rel=0)


@pytest.mark.parametrize("seed", range(18000, 18030))
def test_complete_il_paths_preserve_a_reserved_future_release(seed):
    sim = simulation_with_two_returns(seed, 2, "h2h_one_win")
    on = sim.as_of.astimezone(sim.zone).date()
    sim.transitions["team0"] = (
        RosterMove(add="p8", drop="p0", effective_on=on + timedelta(days=1), starter_games=0),
    )
    plan = return_plan(sim, "team0", ("p0", "p1", "p2"))
    assert plan[0].drop != "p0"
    values = {}
    for first in ("p1", "p2"):
        for second in (*tuple(p for p in ("p1", "p2") if p != first), "p6", "p8"):
            events = (
                (on, 0, "p6", first),
                (on + timedelta(days=1), 1, "p8", "p0"),
                (on + timedelta(days=2), 2, "p7", second),
            )
            oracle = ExplicitTimelineSimulation(sim, events)
            oracle.draws = sim.draws
            values[first, second] = season_value(oracle, "team0", include_playoffs=True)
    assert len(values) == 6
    chosen = tuple(p.drop for p in plan)
    assert chosen in values
    assert values[chosen] == pytest.approx(
        max(values.values()), abs=sim.params.tolerance.value, rel=0
    )


@pytest.mark.parametrize("interruption", ("cancelled", "deadline"))
def test_interrupted_complete_il_search_never_publishes_its_first_candidate(
    monkeypatch, interruption
):
    from fba.inseason import season

    sim = simulation_with_two_returns(18005, 2, "h2h_one_win")
    completed = []
    original = season.season_value

    def interrupt_after_a_real_evaluation(*args, **kwargs):
        value = original(*args, **kwargs)
        completed.append(value)
        if interruption == "cancelled":
            sim.cancelled = lambda: True
        else:
            sim.deadline = (monotonic(), "week")
        return value

    monkeypatch.setattr(season, "season_value", interrupt_after_a_real_evaluation)
    with pytest.raises(CalculationTimeout, match="cancelled|time budget"):
        return_plan(sim, "team0", ("p0", "p1", "p2"))
    assert completed
