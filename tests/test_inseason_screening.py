"""Screening must compare complete plans against one baseline."""

import pytest
from inseason_support import DEFAULTS, fixture

from fba.contracts.inseason import InseasonPreferences
from fba.contracts.inseason_results import RosterMove
from fba.inseason.matchup import Simulation
from fba.inseason.recommendations import candidate_moves


def test_same_day_disjoint_swaps_have_the_same_complete_plan_screening_score():
    sim = Simulation(*fixture())
    prefs = InseasonPreferences.model_validate_json((DEFAULTS / "preferences.json").read_bytes())
    on = sim.as_of.astimezone(sim.zone).date()
    week = next(w for w in sim.league.matchups if w.start <= on <= w.end)
    keys = tuple(c.id for c in sim.league.categories)
    a = RosterMove(add="p6", drop="p0", effective_on=on, starter_games=0)
    b = RosterMove(add="p7", drop="p1", effective_on=on, starter_games=0)
    scores = []
    for first, second in ((a, b), (b, a)):
        candidates = candidate_moves(sim, prefs, sim.roster("team0"), (first,), on, week.end, keys)
        scores.append(next(score for score, moves in candidates if moves[-1] == second))
    assert scores[0] == pytest.approx(scores[1], abs=sim.params.tolerance.value)


def test_search_context_keeps_all_categories_when_none_is_key_and_must_win_skips_ros(monkeypatch):
    from fba.contracts.inseason import MatchupPriority
    from fba.inseason import recommendations

    sim = Simulation(*fixture())
    before = sim.week("team0", "team1", "2")
    before = before.model_copy(
        update={
            "categories": tuple(
                c.model_copy(update={"strategy": "safe"}) for c in before.categories
            )
        }
    )
    keys, future = recommendations.search_context(sim, before)
    assert keys == tuple(c.id for c in before.categories)
    assert future >= 0

    def unexpected(*args, **kwargs):
        raise AssertionError("must-win screening and replay must not request future opponents")

    monkeypatch.setattr(recommendations, "season_value", unexpected)
    before = before.model_copy(
        update={"priority": MatchupPriority(status="must_win", reason="test")}
    )
    assert recommendations.search_context(sim, before) == (keys, 0.0)
