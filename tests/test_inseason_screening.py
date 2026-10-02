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


def test_batch_screening_matches_independent_scalar_equations_and_reuses_moments(monkeypatch):
    from math import erf, sqrt

    import numpy as np

    from fba.inseason.screening import Screening

    sim = Simulation(*fixture())
    on = sim.as_of.date()
    end = next(w.end for w in sim.league.matchups if w.start <= on <= w.end)
    a, _ = sim.total("team0", "2")
    b, _ = sim.total("team1", "2")
    baseline = Screening.create(sim, end, ("PTS",))

    def no_repeated_total(*args, **kwargs):
        raise AssertionError("A screening batch must not recompute baseline teams per candidate")

    monkeypatch.setattr(sim, "total", no_repeated_total)
    for add in ("p6", "p7"):
        for drop in ("p0", "p1", "p2"):
            move = RosterMove(add=add, drop=drop, effective_on=on, starter_games=0)
            players = {p.player.id: p for p in sim.projection(on).players}
            delta = 0.0
            for pid, sign in ((add, 1), (drop, -1)):
                player = players[pid]
                games = sum(
                    g.home == player.player.team_id or g.away == player.player.team_id
                    for g in sim.games
                    if on <= g.tipoff.astimezone(sim.zone).date() <= end
                )
                delta += sign * games * player.expected["PTS"]
            own, other = a[:, sim.axes.index("PTS")], b[:, sim.axes.index("PTS")]
            denominator = sqrt(float(np.var(own) + np.var(other)))
            assert denominator > 0
            before = (
                1 + erf((float(own.mean()) - float(other.mean())) / denominator / sqrt(2))
            ) / 2
            after = (
                1
                + erf(
                    (max(0, float(own.mean()) + delta) - float(other.mean()))
                    / denominator
                    / sqrt(2)
                )
            ) / 2
            assert baseline.score((move,)) == pytest.approx(after - before, abs=1e-12)
